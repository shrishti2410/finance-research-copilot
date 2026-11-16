# Trading signals (version 2, step 7C)

**Educational project, not investment advice.**

A BUY, SELL or HOLD for a stock, with its full evidence. The design rule:
**Python computes every number; the language model never reads a price or
predicts one.**

| layer | does | where |
|---|---|---|
| data | daily prices, cached; NSE tickers in rupees; Indian delivery costs | `market/store.py`, `market/markets.py`, `market/costs.py` |
| knowledge | one card per strategy: rules, regime, when it fails, risk | `docs/strategies/`, table `strategy_cards` |
| signal | regime, weighted vote, decision, evidence | `market/regime.py`, `market/signals.py` |
| evaluation | out of sample against buy-and-hold, the index and random timing | `market/signal_eval.py` |
| explanation | the agent explains the evidence, citing cards | `get_trading_signal` in `agent/tools.py`, `market/explain.py` |

## How a signal is made

Every step is decided at a bar's close, from that bar and the ones before it.
A trade it implies fills at the next open.

1. **Regime.**
   - Measurements: ADX(14), the 20-day change of the 50-day SMA, and 20-day
     volatility ranked against the previous 252 days.
   - Labels:
     - `volatile` at the 80th volatility percentile or above;
     - `trending_up` or `trending_down` at ADX 25 or above;
     - `ranging` below ADX 20;
     - `transition` between 20 and 25.
2. **Votes.** Each strategy is in (+1) or out (−1). On NSE every rule is
   long or flat; the long/short crossover is left out because, without shorts,
   it duplicates the plain crossover.
3. **Walk-forward weights.**
   - Every 21 bars, each rule's weight becomes its Sharpe ratio over the
     previous 504 bars, net of costs, floored at zero.
   - The weights then stay fixed for the next 21 bars.
   - Rule parameters are the textbook defaults and are never fitted.
4. **Regime fit.** Trend rules get half weight in a range, and reversion rules
   half weight in a trend.
5. **Decision.**
   - The score is the weighted mean vote, from −1 to +1.
   - BUY at +0.25 or more, SELL at −0.25 or less, otherwise HOLD.
   - Coverage = the rules carrying weight out of those eligible, shown with
     every signal ("6 of 16 rules").
   - Confidence = |score| × history factor, capped at the coverage fraction.
     It is for display only; the decision does not use it.

**Limited history.** Fewer than 750 bars up to the day means:
- six months of training instead of two years;
- rules whose warm-up exceeds a third of the history are skipped;
- the history factor is 126/504;
- the evidence carries a warning.

All of this is decided per day, from the bars up to that day.

**Today's bar** is ignored until its session has closed: 15:30 IST for NSE,
plus a half-hour margin.

## The evidence

`GET /market/{ticker}/signal` (logged in) returns:
- the signal, score, confidence and thresholds;
- the regime's measurements;
- every vote, with its training Sharpe, weight, regime multiplier, share of
  the vote and card path;
- the skipped strategies, with reasons;
- the history, the method, any warnings, and the disclaimer.

The shares of the vote add up to 1, and the share-weighted votes add up to the
score. `tests/test_signals.py` checks both.

## The explanation (phase 4)

`GET /market/{ticker}/signal/explain`, or `market.explain.explain_signal`.

**How it is produced:**
- The agent is asked to explain the signal, and calls `get_trading_signal`.
  That tool returns:
  - the engine's output, coverage, regime and heaviest votes;
  - the **backtest verdict**;
  - the Summary and "When it fails" sections of the voting rules' cards, read
    from the `strategy_cards` collection.
- The tool's description tells the model to:
  - report the signal as the engine's output, never as advice;
  - name each card by its path;
  - state the backtest verdict plainly.
- The answer goes through the unchanged grounding and advice guards.

**The verdict** (`market.signal_eval.backtest_verdict`):
- An **edge** needs all three over five years: a higher CAGR than
  buy-and-hold, a higher daily Sharpe, and beating 95% of the random
  re-timings.
- Anything less is **no edge**. The sentence saying so is written in Python
  from the evaluation's own figures.

**Two checks after the guards:**
- When the verdict is no edge and the answer does not say "no edge", the
  tool's sentence is put first, verbatim.
- The disclaimer is appended if missing.

Both are listed in the result's `added`, so a model that left them out is
visible.

**Which model:** local Ollama only (`qwen2.5:7b`, with `qwen2.5:1.5b` for tool
selection). A hosted provider needs the owner's approval.

## How well it works

See `benchmarks/SIGNALS_7C.md`.
- Over 2021-11-16 to 2026-11-16, on four NSE stocks, the signal returned less
  than buy-and-hold on all four.
- It had smaller drawdowns on three, mostly by being out of the market about
  half the time.
- Against its own randomly re-timed copies it did not stand out (39th, 76th,
  0th and 72nd percentiles).

Treat the signal as a structured summary of what 16 textbook rules currently
say, not as a forecast.
