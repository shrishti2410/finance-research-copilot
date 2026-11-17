# Quant honesty: what is measured, and the adoption rules, written before the run

Written and committed on 2026-11-17, before any of the computations below were
run on real prices. The results go in `benchmarks/QUANT_HONESTY.md`. The
commit that adds this file comes before the commit that adds the code's first
real-data output, so the order can be checked in `git log`.

## The data

- **Stocks:** the 15 NSE stocks already evaluated in
  [SIGNALS_7C.md](SIGNALS_7C.md):
  - the 5 watchlist stocks, used while building the signal;
  - the 10 hold-out stocks in `market/universe.py`, never used to build or
    tune anything.

  The US stocks are not included. They were added later, have a different
  voting rule for shorts, and were not part of the evaluation the published
  claims rest on.
- **Window:** the last 5 years up to the close of **2026-11-13** (a Friday),
  out of sample, as in `market/signal_eval.py`. The window is frozen so that a
  rerun on a later day gives the same numbers.
- **Prices and costs:** prices from the local price cache. Indian delivery
  costs and 5 bps slippage, next-open execution: the same backtester and costs
  as the published signal.

## What is computed, for every stock

The strategies and benchmarks:

| strategy | what it is |
|---|---|
| **signal** | the production signal: Sharpe-weighted votes with regime multipliers, monthly walk-forward refit (`market/signals.py`). In the ablation this is the **regime-weighted** ensemble. |
| **performance-weighted** | the same, without the regime multipliers |
| **equal-weight** | every eligible rule's vote counts the same; same BUY/SELL thresholds (+0.25/-0.25), HOLD keeps the position |
| **majority** | in when more than half of the eligible rules are in, out otherwise |
| **best single rule (walk-forward)** | at each monthly refit, the rule with the highest trailing Sharpe; its position until the next refit |
| **best single rule (hindsight)** | the rule with the best Sharpe over the window itself. This is look-ahead and is reported only as an upper bound, never as a candidate. |
| **buy-and-hold** | the stock, the same costs |
| **exposure-matched** | buy-and-hold scaled to the signal's average exposure over the window, the rest in cash at 0% |

- **"Eligible"** means the rules carrying a weight at that month's refit
  (warmed up, enough history), as in production.
- **Exposure-matched, the formula:** daily return = e × the stock's daily
  return, with e fixed for the whole window. Scaling leaves the Sharpe ratio
  unchanged (0% cash), so it answers a different question: **are the signal's
  smaller drawdowns and lower CAGR what its lower exposure would give anyway?**

**Metrics for each strategy**, net of costs unless stated:
- CAGR;
- maximum drawdown;
- Sharpe and Sortino, with no risk-free rate, as `risk_metrics`;
- exposure: the share of days in the stock;
- turnover: position changes a year;
- cost drag: CAGR with zero costs and slippage, minus CAGR net.

**Rule correlation:** the correlation matrix of the 16 voting rules' daily net
returns over the window, per stock, then averaged across the 15 stocks.

## Adoption rules (decided now, applied mechanically afterwards)

1. **An alternative ensemble replaces the production signal only if all three
   hold:**
   - (a) its median Sharpe across the 15 stocks exceeds the signal's by at
     least 0.10;
   - (b) it has the higher Sharpe on at least 10 of the 15 stocks;
   - (c) it has the higher Sharpe on at least 7 of the 10 hold-out stocks.

   The hindsight best rule is never a candidate. Even a pass is a proposal:
   changing the product needs the user's approval, on its own branch.
2. **"Risk-adjusted edge over buy-and-hold"** may be claimed only if the
   signal has the higher Sharpe on at least 10 of the 15 stocks **and** the
   median Sharpe difference is above zero. Otherwise the site and README keep
   saying **no edge**.
3. **"Smaller drawdowns"** may be claimed only if the signal's maximum
   drawdown is smaller than the **exposure-matched** benchmark's on at least
   10 of the 15 stocks. Otherwise the claim is withdrawn wherever it appears,
   with the reason that the drawdown reduction is explained by lower exposure.
4. **If rule 2 fails:** the recommendation recorded with the results is to
   present the signal as a strategy-audit tool (what the rules say and how they
   would have done), not as a source of edge. It is a recommendation for the
   user, not a change made in this branch.
5. **Correlation:** pairs of rules with an average correlation above 0.80 are
   reported as redundant. Nothing is removed on the strength of this run.
6. **Ties and gaps:**
   - A stock where a strategy has no Sharpe (no trades, or constant equity)
     counts as **not higher** for that strategy.
   - Every count is reported out of 15 (and out of 10 for the hold-out stocks),
     including any stock that could not be evaluated, with the reason.

Nothing in these rules may be changed after the first real-data run. If one
turns out to be badly specified, the results are reported under the rule as
written, and any new rule is written down separately and applied only to
untouched data.
