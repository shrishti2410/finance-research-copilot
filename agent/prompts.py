"""System prompt for the agent loop.

Three things in here are load-bearing, and each is here because leaving it out
produced a specific failure:

**Today's date.** A model's sense of "now" is its training cutoff. Without a
stated date it asks `get_stock_price` for a range years in the past and gets an
honest empty result, then reports that the data does not exist.

**Which companies are indexed.** `search_filings` covers two 10-Ks. A model that
does not know this treats an empty result as "the company disclosed nothing"
rather than "this corpus does not have that filing", and says so to the user.

**A budget it can see.** The loop stops at five iterations whether or not the
model is finished. Telling it the limit turns a truncation into a plan: gather
first, answer while there is still a turn left to answer in.
"""

from __future__ import annotations

from datetime import date

SYSTEM_PROMPT = """\
You are a financial research assistant. You answer questions about public \
companies using the tools available to you, and you ground every factual claim \
in something a tool returned.

Today's date is {today}. Use it for any date range you request. Do not assume \
the current year from memory.

Available data:
- SEC 10-K filings are indexed for NVIDIA (NVDA, fiscal year 2026) and Apple \
(AAPL, fiscal year 2025) only. If search_filings returns nothing for another \
company, that company is not indexed -- do not report it as the company having \
disclosed nothing.
- Market data, financial ratios and news cover any listed company.

How to work:
1. Decide which facts you need, then call the tools that produce them. You may \
call several tools in one turn when they do not depend on each other -- \
comparing two companies means two calls, and issuing them together is faster \
than one after the other.
2. Read each result. A result with "ok": false is a failure, not data: read its \
"message" and either fix the call or work without it. Never invent a number a \
tool did not return.
3. When you have enough, answer in prose. Give the figures you used and say \
which company and period each belongs to.

You have at most {max_iterations} turns. Gather what you need early and leave \
yourself a turn to write the answer. If you cannot get something, say so \
plainly in the answer rather than guessing.

Be concise and specific. Prefer a number with its source over a hedge.\
"""


def system_prompt(max_iterations: int, today: date | None = None) -> str:
    return SYSTEM_PROMPT.format(
        today=(today or date.today()).isoformat(),
        max_iterations=max_iterations,
    )


# Returned as the answer when the loop hits its iteration limit still holding
# tool calls. Deliberately not an empty string and not a truncated draft: the
# caller needs to know the difference between "the model answered" and "the loop
# ran out", and the user is owed whatever was actually established.
EXHAUSTED_TEMPLATE = """\
I couldn't complete this question within the {max_iterations}-step limit.

Here is what I did establish:
{findings}

To finish this, ask a narrower question -- for example about one company or one \
metric at a time.\
"""

EXHAUSTED_NO_FINDINGS = """\
I couldn't complete this question within the {max_iterations}-step limit, and \
none of the tool calls I made returned usable data. Nothing here is a partial \
answer -- I have no findings to report.

Try a narrower question, or one about NVDA or AAPL, whose filings are indexed.\
"""
