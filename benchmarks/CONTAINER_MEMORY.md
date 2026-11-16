# API container memory and CPU under the free host's limits

**Question.** Does the API, with its torch embedding model, fit in Render Free's
512 MB and 0.1 CPU while it answers a real question?

**Answer: memory fits, CPU needs one setting.**
- Memory: peak 414.6 MiB of 512 MiB while answering a filing question; no
  out-of-memory kill. torch stays; the ONNX change is not needed.
- CPU: at 0.1 CPU, torch's default thread count made each query embedding take
  21 to 55 s. With one thread (`EMBEDDING_THREADS=1`) it took 0.2 to 0.6 s.

## How it was run

| | |
|---|---|
| date | 2026-11-16, 10:43 to 11:30 UTC |
| commit | `bcdab3e` (`feat/deploy-prep`), clean tree; image `copilot-backend:membench`, 2.44 GB |
| machine | Windows 11 laptop, 16 GB, 12 logical CPUs; Docker 29.9.3 (Linux VM); laptop awake throughout |
| model | local Ollama 0.40.16, `qwen2.5:7b` answers, `qwen2.5:1.5b` router. No hosted tokens were spent |
| limits | `docker run --memory=512m --memory-swap=512m`, plus `--cpus=0.1` for the CPU runs |
| started with | `scripts/start.sh` (migrate, then serve), against the laptop's Postgres, Redis and Ollama |
| memory figures | the container's own cgroup files: `memory.peak`, `memory.current`, `memory.events` |
| script | the commands are in `benchmarks/results/2026-11-16-container-memory/run_env.txt` and the files beside it |

## Memory, full CPU, one real question

The question was "What does NVIDIA's 10-K say about the risk from U.S. export
controls? Quote the filing." It needs `search_filings`, so the embedding model is
loaded.

| run | memory after start | memory peak | OOM kills | answer |
|---|---:|---:|---:|---|
| 1, 10:43 UTC, machine busy indexing | not recorded (a shell path bug) | 414.5 MiB | 0 | model read timeout (300 s) |
| 1b, 11:20 UTC, idle | 144.8 MiB | 414.6 MiB | 0 | model read timeout (300 s) |
| 1c, 11:27 UTC, idle, `INFERENCE_READ_TIMEOUT=900` | | **414.6 MiB** | **0** | **answered**, quoting Item 1A, in 200 s |

The timeouts in runs 1 and 1b were the local 7B model on the laptop's CPU, not
the container. Memory was the same in all three, because the search, which loads
the embedding model, had already run.

Excerpt from `memory_512m_rerun2.txt`:

```
status 200 stop final_answer
answer wall time 200 s
after question: memory.current 424587264 memory.peak 434692096 bytes (limit 536870912)
oom 0
oom_kill 0
```

## CPU at 0.1, query embeddings only

`docker run --cpus=0.1 --memory=512m`, timing `rag.embeddings.embed_query` three
times in a fresh process.

| torch threads | first call, including import and model load | second | third | peak memory |
|---|---:|---:|---:|---:|
| default (6) | 445.3 s | 21.00 s | 22.40 s | 314.3 MiB |
| 1 (`EMBEDDING_THREADS=1`, `OMP_NUM_THREADS=1`) | 108.0 s | 0.20 s | 0.20 s | 297.5 MiB |

An earlier run of the same probe the same morning, not saved to a file, gave
55 s per query with the default threads and 0.4 to 0.6 s with one thread. The
spread between the two runs is large because the laptop was busy with other
work during the saved run. Read the result as a range: tens of seconds with the
default threads, under a second with one.

## Not measured

- Render itself. Its 0.1 CPU may not behave like Docker's `--cpus=0.1`.
- A hosted-model answer inside the capped container. No Groq tokens were spent.
