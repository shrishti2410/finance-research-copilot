# benchmarks/

Measurements that inform the project without being part of it. Nothing here is
imported by the app, runs in CI, or changes a default. It is deliberately outside
the deployment: the backend image's `COPY` list does not include this directory,
so none of it ships.

## `colab_t4_decode_benchmark.ipynb`

Upload to [Google Colab](https://colab.research.google.com), set **Runtime →
Change runtime type → T4 GPU**, and run all cells. Free tier; about five minutes,
most of it downloading the model.

It runs the hand-written greedy decode loop from milestone 1
(`llm-internals/transformer_walkthrough.py`, which lives beside this repo rather
than in it) on Colab's GPU, then on Colab's CPU, and compares both against the
numbers measured on the dev host.

The question it answers: **how much of this project's latency is the absence of a
GPU?** Everything about the app's inference configuration — the 1.5b/7b router
split, the 512-token generation cap, accepting +25% end-to-end to raise `num_ctx`
for correctness — follows from `docs/INFERENCE.md`'s finding that generation is
~97% of request latency at 2.8–4.2 tok/s. This puts a number on how much of that
is the hardware.

### Two things the port had to change, and why

A naive copy of the CPU loop onto a GPU reports nonsense. Both fixes are in the
notebook with the reasoning inline:

- **`torch.cuda.synchronize()` around every timed step.** CUDA kernels are
  asynchronous, so timing them the way the CPU version does measures how fast
  Python can *queue* work. Without this you get thousands of tokens/sec and a
  fiction.
- **`float16`, not `bfloat16`.** The original picks bf16 on CUDA, which is right
  for Ampere and later. A T4 is Turing (7.5) with no bf16 tensor cores, so torch
  accepts it and emulates it slowly. The notebook reads the compute capability
  and picks accordingly rather than assuming.

There is also a warm-up pass excluded from all timings, because the first CUDA
forward pass pays for context creation and kernel autotuning and would otherwise
land entirely on step 0.

### Reading the result

The honest comparison is GPU vs CPU **on the same machine**, which the notebook
measures directly. Comparing its GPU number against the app's 7B Ollama figures
moves two variables at once — model size and quantized llama.cpp vs unquantized
transformers — so that ratio is a ceiling on what a GPU would buy the product,
not an estimate of it. Batch size is 1 throughout, which leaves a GPU almost
idle; that understates what a batching server like vLLM gets from the same card.

Section 8 of the notebook states the caveats in full.
