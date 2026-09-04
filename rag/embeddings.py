"""Sentence embeddings for filing chunks and for queries.

`BAAI/bge-small-en-v1.5`: 384 dimensions, 33M parameters, 512-token context. It
is here because it runs on CPU at a usable rate, and because 384-dim vectors
cost a quarter of what the 1536-dim vectors from the hosted APIs cost to store
and to compare -- which matters more for a corpus this size than the last point
of MTEB score does.

No `sentence-transformers` dependency. For bge-small that library's entire
forward pass is "run the encoder, take the CLS token, L2-normalize it", which is
what `_encode` below does; torch and transformers are already required, and
sentence-transformers is not a small thing to add for thirty lines of pooling.

Two things about this model are easy to get wrong and expensive to notice:

**BGE is asymmetric.** Queries take an instruction prefix, passages do not.
Embedding a passage *with* the prefix, or a query *without* it, degrades recall
silently -- nothing errors, the scores just get worse. So there are two entry
points, `embed_passages` and `embed_query`, rather than one `embed()` with a
flag that a caller can forget.

**Vectors come out L2-normalized**, which makes cosine similarity and inner
product the same number. `rag/vector_store.py` depends on that: it is what lets
the pgvector and array-fallback backends compute byte-identical scores.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384

# The encoder's hard limit. Our chunks are sized in *Qwen* tokens and the two
# tokenizers do not agree, so a passage that fits by one count can overflow by
# the other; `_windows` handles that rather than letting the tail be dropped.
MAX_SEQUENCE_TOKENS = 512

# The prefix BGE was trained with for retrieval. Queries only. The model card
# notes it matters most for short queries, which is exactly what we send.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# Overlap between windows when a passage exceeds MAX_SEQUENCE_TOKENS.
LONG_INPUT_OVERLAP = 64

DEFAULT_BATCH_SIZE = 16


@lru_cache(maxsize=2)
def get_encoder(model_id: str = EMBEDDING_MODEL):
    """Tokenizer and model, loaded once per process.

    Imported lazily: `import rag.embeddings` should not pull torch into a
    process that only wants `EMBEDDING_DIM`.
    """
    import torch
    from transformers import AutoModel, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModel.from_pretrained(model_id)
    model.eval()
    torch.set_grad_enabled(False)
    return tokenizer, model


def _forward(texts: list[str], model_id: str, batch_size: int) -> np.ndarray:
    """One vector per text, truncating anything over the model's limit."""
    import torch

    tokenizer, model = get_encoder(model_id)
    out: list[np.ndarray] = []

    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        encoded = tokenizer(
            batch,
            padding=True,
            truncation=True,
            max_length=MAX_SEQUENCE_TOKENS,
            return_tensors="pt",
        )
        with torch.inference_mode():
            hidden = model(**encoded).last_hidden_state

        # CLS pooling: BGE is trained with the [CLS] position as the sentence
        # representation. Mean pooling here would be a different model.
        cls = hidden[:, 0]
        cls = torch.nn.functional.normalize(cls, p=2, dim=1)
        out.append(cls.cpu().numpy().astype(np.float32))

    if not out:
        return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
    return np.vstack(out)


def _windows(text: str, model_id: str) -> list[str]:
    """Split an over-long text into overlapping windows that each fit the encoder.

    Chunks are sized in *Qwen* tokens, and BGE's tokenizer disagrees -- a
    500-token chunk measured one way runs to ~560 measured the other. Left to
    the tokenizer, the tail past 512 is dropped with nothing worse than a
    warning, which costs recall on exactly the longest, densest passages.

    Windowing keeps the whole passage in its vector. The overlap exists so a
    sentence spanning a window boundary is whole in at least one of them.
    """
    tokenizer, _ = get_encoder(model_id)
    ids = tokenizer(text, add_special_tokens=False)["input_ids"]

    # Two positions of the budget go to [CLS] and [SEP].
    body = MAX_SEQUENCE_TOKENS - 2
    if len(ids) <= body:
        return [text]

    stride = body - LONG_INPUT_OVERLAP
    pieces = [ids[i:i + body] for i in range(0, len(ids), stride)]
    # The final window can be entirely contained in the previous one, in which
    # case it contributes nothing but dilutes the average.
    if len(pieces) > 1 and len(pieces[-1]) <= LONG_INPUT_OVERLAP:
        pieces.pop()
    return [tokenizer.decode(piece, skip_special_tokens=True) for piece in pieces]


def _encode(texts: list[str], model_id: str, batch_size: int) -> np.ndarray:
    """Encode texts to unit-length vectors, windowing anything over the limit.

    A windowed text becomes the L2-normalized mean of its window vectors. The
    mean of unit vectors is not itself a unit vector, so it is renormalized --
    `rag/vector_store.py` computes cosine as a plain dot product and would
    otherwise score long passages systematically low.
    """
    if not texts:
        return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)

    # Flatten to segments, remembering which text each came from, so every
    # segment still rides in a full batch.
    segments: list[str] = []
    owners: list[int] = []
    for index, text in enumerate(texts):
        for window in _windows(text, model_id):
            segments.append(window)
            owners.append(index)

    vectors = _forward(segments, model_id, batch_size)

    if len(segments) == len(texts):
        return vectors

    pooled = np.zeros((len(texts), EMBEDDING_DIM), dtype=np.float32)
    for vector, owner in zip(vectors, owners):
        pooled[owner] += vector
    norms = np.linalg.norm(pooled, axis=1, keepdims=True)
    return (pooled / np.maximum(norms, 1e-12)).astype(np.float32)


def embed_passages(
    texts: list[str],
    model_id: str = EMBEDDING_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
    progress=None,
) -> np.ndarray:
    """Embed documents for indexing. No instruction prefix -- see module docstring.

    Returns an (n, 384) float32 array of unit vectors.
    """
    if not texts:
        return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)

    if progress is None:
        return _encode(texts, model_id, batch_size)

    # Batched with a callback, so a caller can report progress on a run that
    # takes minutes on CPU instead of printing nothing for the whole time.
    chunks: list[np.ndarray] = []
    done = 0
    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        chunks.append(_encode(batch, model_id, batch_size))
        done += len(batch)
        progress(done, len(texts))
    return np.vstack(chunks)


def embed_query(text: str, model_id: str = EMBEDDING_MODEL) -> np.ndarray:
    """Embed a search query, with the retrieval instruction prefix applied.

    Returns a (384,) float32 unit vector.
    """
    return _encode([QUERY_PREFIX + text], model_id, batch_size=1)[0]


# ─────────────────────────────────────────────────────────────────────────────
# Instrumentation
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EmbedTiming:
    """What an embedding run actually cost. Reported, not estimated."""

    count: int
    seconds: float
    windowed: int           # passages too long for one pass, embedded in windows
    max_input_tokens: int

    @property
    def per_item_ms(self) -> float:
        return (self.seconds / self.count * 1000) if self.count else 0.0

    @property
    def per_second(self) -> float:
        return (self.count / self.seconds) if self.seconds else 0.0


def input_token_lengths(texts: list[str], model_id: str = EMBEDDING_MODEL) -> list[int]:
    """Full token length of each text under *this* model's tokenizer.

    Chunks are sized with the Qwen tokenizer, so their length under BGE is a
    different number. Anything over MAX_SEQUENCE_TOKENS is embedded in windows.
    """
    tokenizer, _ = get_encoder(model_id)
    encoded = tokenizer(texts, padding=False, truncation=False)["input_ids"]
    return [len(ids) for ids in encoded]


def embed_passages_timed(
    texts: list[str],
    model_id: str = EMBEDDING_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
    progress=None,
) -> tuple[np.ndarray, EmbedTiming]:
    """`embed_passages`, plus a wall-clock measurement and a windowing count.

    The model is loaded before the clock starts: first-call load is a one-off
    cost of a few seconds and folding it into the throughput number would
    understate steady-state performance by a lot on a run this small.
    """
    get_encoder(model_id)

    lengths = input_token_lengths(texts, model_id) if texts else [0]

    started = time.perf_counter()
    vectors = embed_passages(texts, model_id, batch_size, progress)
    elapsed = time.perf_counter() - started

    return vectors, EmbedTiming(
        count=len(texts),
        seconds=elapsed,
        windowed=sum(1 for n in lengths if n > MAX_SEQUENCE_TOKENS - 2),
        max_input_tokens=max(lengths),
    )
