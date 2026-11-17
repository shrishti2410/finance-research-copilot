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

**After the guards:**
- The verdict sentence always opens the explanation, verbatim.
  `model_said_no_edge` records whether the model also said it. The model's
  own restatement has been seen to get the reason or a figure wrong.
- The supplied card files are listed if the answer names none.
- The disclaimer is appended if missing.

Each addition is listed in the result's `added`. The DI lines are in the
tool output, so the model has no reason to guess them.

**Which model:** local Ollama only (`qwen2.5:7b`, with `qwen2.5:1.5b` for tool
selection). A hosted provider needs the owner's approval.

## Options (phase 5)

`GET /market/{ticker}/options`, or `market.service.option_ideas`.
- Prices the structures whose market view matches the engine's output: BUY →
  bull call spread, long call, covered call; SELL → the bearish mirror; HOLD →
  iron condor, covered call, straddle; a volatile regime adds the straddle.
  `?structure=` asks for one by name.
- Each structure comes with its legs, a premium estimate per leg, net premium,
  payoff at expiry on a grid from −30% to +30%, breakevens, and maximum profit
  and loss (exact, or flagged unbounded).
- The signal's coverage and backtest verdict travel with the structures.

**The premiums are Black-Scholes estimates**, from the stock's own 30-day
historical volatility, not market quotes. The other assumptions, each
returned with every result:
- a risk-free rate of 6.5%, an assumed round figure that was not looked up;
- no dividends; European exercise;
- strikes are round numbers near the price, not NSE's listed strikes;
- figures are per share (`lot_size` scales them; NSE lot sizes are not
  guessed);
- option trading costs are not included.

`market/options.py`; tests against the textbook Black-Scholes case,
put-call parity and each card's own max-loss and breakeven formulas.

## How well it works

See `benchmarks/SIGNALS_7C.md`.
- Over 2021-11-16 to 2026-11-16, on four NSE stocks, the signal returned less
  than buy-and-hold on all four.
- Its drawdowns were shallower on three. That is what being out of the market
  about half the time gives: against buy-and-hold held at the same exposure,
  its drawdown was smaller on only 2 of 15 NSE stocks
  (`benchmarks/QUANT_HONESTY.md`). So no drawdown benefit is claimed.
- Against its own randomly re-timed copies it did not stand out (39th, 76th,
  0th and 72nd percentiles).

Treat the signal as a strategy-audit tool: a structured summary of what 16
textbook rules currently say and how they would have done. It is not a
forecast, and not a source of edge.
