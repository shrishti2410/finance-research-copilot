# Eval baselines

`current.json` is the reference the agent is compared against. It is a run's
per-case output plus the metadata needed to interpret it.

```bash
# run the eval and say whether it got better, worse or stayed flat
python -m eval.compare

# same, against a run already captured
python -m eval.compare --report eval_run.json

# make a run the new reference
python -m eval.compare --promote eval_run.json --name my-change --note "what this measured"
```

`python -m eval.compare` exits non-zero only when the run got **worse**, so it
can gate a change without failing on noise.

## What a baseline stores, and why

**Answers, not verdicts.** Both sides are rescored with the current
`eval/metrics.py` before being compared. Run A's stored verdicts included two
that were the extractor's fault and not the agent's — a revenue answer read as
the gross-profit figure quoted earlier in the same sentence, and `January 31,
2026` read as $31.00. Freezing those would have made fixing the extractor look
like the agent gaining two cases. Rescoring both sides means a scorer change
moves both numbers and cancels out.

`scored_at_save` records what the baseline scored when it was taken. If that
differs from what it scores now, the comparison says so — the scorer has moved,
and you should know.

**The configuration it measured.** `system.recorded_by_the_run` tells you whether
that block is fact or a guess. Reports written before `run_eval` recorded its own
configuration get today's settings stamped on them with the flag set to `false`;
do not read those as what the run measured. The current baseline is one of these
— see its `note`.

## The verdict band

40 cases means one case is 2.5 points, and this eval is not deterministic. Two
consecutive runs of the *unchanged* system scored 38.9% and 33.3% over the first
18 cases and disagreed on five of them. So `--min-cases` (default 2) is how many
net cases a move must clear before it is called a direction.

Inside the band the verdict is `flat` — but every case that moved is still listed
in both directions. A net-zero run where two cases broke and two unrelated ones
were fixed is flat by the number and worth reading in full.

## What this does not tell you

Accuracy over 40 questions, and cost. It does not cover groundedness (see
`eval/judge.py`) or retrieval recall (`eval/retrieval_eval.py`), and a change
that improves accuracy while inventing more unsourced figures would look like an
improvement here.
