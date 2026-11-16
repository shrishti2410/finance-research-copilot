"""Strategy cards: the knowledge the signal explanations cite.

Each card in docs/strategies/ describes one strategy in plain words: what it
does, its rules, the market it suits, when it fails, its risk, its parameters
and where its code is. The signal engine computes every number; the cards
are what the agent quotes to explain *why* a rule says what it says.

    cards = load_cards()                     # parsed and validated
    async with engine.begin() as conn:
        await index_cards(conn, cards)       # one row per card section
        hits = await search_cards(conn, "what goes wrong in a sideways market")

A separate collection
---------------------
The cards live in their own table, `strategy_cards` (migration 0007), not in
`filing_chunks`. A filing search can therefore never return a strategy card,
and a card search never returns a 10-K passage. They are different kinds of
evidence: one is a company's own disclosure, the other this project's
explanation of a trading rule.

One row per card section, embedded with the same bge-small model as the
filings. The section's text is prefixed with the card's name, so a question
about "the golden cross failing" finds the golden-cross card's "When it
fails" section rather than any card's. About 180 rows: an exact cosine scan
is a fraction of a millisecond, so there is no HNSW index to maintain.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
from sqlalchemy import text

from rag.embeddings import embed_passages, embed_query

__all__ = ["Card", "CardHit", "CardError", "CARD_DIR", "REQUIRED_SECTIONS", "KINDS",
           "REGIMES", "parse_card", "load_cards", "index_cards", "search_cards",
           "card_sections"]

CARD_DIR = Path(__file__).resolve().parents[1] / "docs" / "strategies"
TABLE = "strategy_cards"
REQUIRED_SECTIONS = ("Summary", "Rules", "Regime", "When it fails", "Risk",
                     "Parameters", "Code")
KINDS = ("equity", "option")
REGIMES = ("any", "trending", "ranging", "downtrend", "volatile")
_FRONT = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)


class CardError(ValueError):
    """A card file that does not follow the format."""


@dataclass(frozen=True)
class Card:
    key: str
    name: str
    kind: str
    regime: str
    code: str                       # "market/strategies.py#key", or "pending"
    sections: dict[str, str]
    path: str
    view: str | None = None         # options: the market view, e.g. "bullish"

    @property
    def source(self) -> str:
        return f"docs/strategies/{Path(self.path).name}"


@dataclass(frozen=True)
class CardHit:
    card_key: str
    name: str
    kind: str
    regime: str
    section: str
    content: str
    source: str
    score: float
    extra: dict = field(default_factory=dict)


def parse_card(raw: str, path: str = "<card>") -> Card:
    raw = raw.replace("\r\n", "\n")
    match = _FRONT.match(raw)
    if not match:
        raise CardError(f"{path}: no front matter between --- lines")
    meta = {}
    for line in match.group(1).splitlines():
        name, sep, value = line.partition(":")
        if not sep:
            raise CardError(f"{path}: front matter line without a colon: {line!r}")
        meta[name.strip()] = value.strip()
    for required in ("key", "name", "kind", "regime", "code"):
        if not meta.get(required):
            raise CardError(f"{path}: front matter needs {required!r}")
    if meta["kind"] not in KINDS:
        raise CardError(f"{path}: kind {meta['kind']!r} is not one of {KINDS}")
    if meta["regime"] not in REGIMES:
        raise CardError(f"{path}: regime {meta['regime']!r} is not one of {REGIMES}")

    sections: dict[str, str] = {}
    current = None
    for line in match.group(2).splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = ""
        elif current is not None:
            sections[current] += line + "\n"
    sections = {k: v.strip() for k, v in sections.items()}
    missing = [s for s in REQUIRED_SECTIONS if not sections.get(s)]
    if missing:
        raise CardError(f"{path}: missing or empty section(s): {', '.join(missing)}")
    return Card(key=meta["key"], name=meta["name"], kind=meta["kind"],
                regime=meta["regime"], code=meta["code"], sections=sections,
                path=str(path), view=meta.get("view"))


def load_cards(directory: Path = CARD_DIR) -> list[Card]:
    cards = []
    for path in sorted(Path(directory).glob("*.md")):
        if path.name.upper() == "README.MD":
            continue
        card = parse_card(path.read_text(encoding="utf-8"), str(path))
        if card.key != path.stem:
            raise CardError(f"{path}: key {card.key!r} does not match the file name")
        cards.append(card)
    return cards


def _passage(card: Card, section: str, body: str) -> str:
    """What is embedded: the card's name (and an option's market view) with
    the section, so a question finds the right card, not just any card's
    section of that name."""
    view = f" (view: {card.view})" if card.view else ""
    return f"{card.name}{view}: {section}. {body}"


def _vector(v) -> str:
    return "[" + ",".join(f"{float(x):.7g}" for x in v) + "]"


_VEC = "CAST(CAST(:embedding AS text) AS vector)"


async def index_cards(conn, cards: list[Card],
                      embed: Callable[[list[str]], np.ndarray] = embed_passages) -> int:
    """Replace the collection with these cards. Returns the rows written.

    A full replace: the collection is small, and a card that was renamed or
    deleted must not linger as evidence."""
    rows = [(card, section, body) for card in cards for section, body in card.sections.items()]
    vectors = embed([_passage(c, s, b) for c, s, b in rows])
    await conn.execute(text(f"DELETE FROM {TABLE}"))
    if rows:
        await conn.execute(text(
            f"INSERT INTO {TABLE} (card_key, name, kind, regime, section, content, "
            f"source_path, content_hash, embedding) VALUES (:card_key, :name, :kind, "
            f":regime, :section, :content, :source_path, :content_hash, {_VEC})"),
            [{"card_key": c.key, "name": c.name, "kind": c.kind, "regime": c.regime,
              "section": s, "content": b, "source_path": c.source,
              "content_hash": hashlib.sha256(b.encode()).hexdigest(),
              "embedding": _vector(v)} for (c, s, b), v in zip(rows, vectors)])
    return len(rows)


async def search_cards(conn, query: str, k: int = 5, *, kind: str | None = None,
                       card: str | None = None, section: str | None = None,
                       embed: Callable[[str], np.ndarray] = embed_query) -> list[CardHit]:
    """The k card sections closest to `query` by cosine similarity, optionally
    limited to one kind ("equity", "option"), one card, or one section."""
    where, params = [], {"embedding": _vector(embed(query)), "k": k}
    if kind:
        where.append("kind = :kind")
        params["kind"] = kind
    if card:
        where.append("card_key = :card")
        params["card"] = card
    if section:
        where.append("section = :section")
        params["section"] = section
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = (await conn.execute(text(
        f"SELECT card_key, name, kind, regime, section, content, source_path, "
        f"1 - (embedding <=> {_VEC}) AS score FROM {TABLE} {clause} "
        f"ORDER BY embedding <=> {_VEC} LIMIT :k"), params)).all()
    return [CardHit(card_key=r[0], name=r[1], kind=r[2], regime=r[3], section=r[4],
                    content=r[5], source=r[6], score=round(float(r[7]), 4)) for r in rows]


async def card_sections(conn, key: str) -> dict[str, str]:
    """Every section of one card, in order, from the index."""
    rows = (await conn.execute(text(
        f"SELECT section, content FROM {TABLE} WHERE card_key = :k ORDER BY id"),
        {"k": key})).all()
    return {r[0]: r[1] for r in rows}
