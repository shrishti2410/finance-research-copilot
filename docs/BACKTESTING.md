# Strategies and backtesting (version 2, step 7B)

`market/strategies.py` holds 13 strategies behind one interface: twelve
classics, including buy-and-hold as the benchmark, and a long/short variant of
the crossover. `market/backtest.py` runs any of them on daily bars with
transaction costs, slippage and next-bar execution, and returns buy-and-hold on
the same terms.

## Library or our own: the licence check

Checked on 2026-11-16 from each project's own repository and package pages.
Nothing was installed.

| | licence | for this project |
|---|---|---|
| **vectorbt** | Apache 2.0 **with the Commons Clause** (`LICENSE.md`): "the License does not grant to you, the right to Sell the Software", where selling includes hosting "whose value derives, entirely or substantially, from the functionality of the Software" | A hosted demo built on it would be close to that line. Its dependencies (numba, scipy, scikit-learn, plotly, pandas 3) also do not fit an API with 512 MB. |
| **backtesting.py** | **AGPL-3.0** (0.6.6, July 2026). Section 13 requires offering the source of a modified version to everyone who uses it over a network | The obligations reach the whole served application. |
| **our own** | this repository's | A daily long/flat/short engine is under 200 lines. Every assumption is in view and tested. |

**Decision: our own.** What it does not do, and the libraries do: intraday bars,
portfolios of several assets, order types beyond market-on-open, position
sizing other than all-in, and parameter optimisation.

## The rules the engine follows

- **Next-bar execution.** A strategy decides at bar *t*'s close. The trade is
  filled at bar *t+1*'s open, never at the close it was computed from.
- **No look-ahead in the strategies either.** `tests/test_backtest.py` runs
  every strategy on history cut at several points. It checks that no past
  position changes when later bars are added.
- **Costs.** Commission (`cost_bps`) and slippage (`slippage_bps`) are charged
  in basis points of the traded notional, on each side. Going from long to
  short trades twice the equity. A recorded fill is the open, moved against the
  trader by the slippage.
- **Adjusted prices.** Open, high, low and close are scaled by `adj_close /
  close`, so dividends are earned and a split is not a crash. Buy-and-hold uses
  the same prices.
- **Warm-up.** Bars before `start_day` are read by the strategy but not traded,
  so a 200-day rule can hold a position on the first day of the window, as
  buy-and-hold does. The API fetches 400 calendar days of warm-up.
- **Exposure.** The whole equity is committed at 1x long or short, or the
  strategy is flat. There is no interest on cash and no borrow cost for shorts.

## The strategies

| key | rule (defaults) |
|---|---|
| `buy_and_hold` | always long; the benchmark |
| `sma_cross` | long while SMA 50 > SMA 200 |
| `ema_cross` | long while EMA 12 > EMA 26 |
| `price_above_sma` | long while close > SMA 200 |
| `macd_cross` | long while MACD > its signal line (12, 26, 9) |
| `rsi_reversion` | buy RSI(14) < 30, sell > 70 |
| `bollinger_reversion` | buy a close below the lower band (20, 2σ), sell above the middle |
| `bollinger_breakout` | buy above the upper band, sell below the middle |
| `donchian_breakout` | buy above the prior 20-day high, sell below the prior 10-day low (Turtle) |
| `momentum` | long while the return from 252 to 21 days ago is positive (12-1) |
| `zscore_reversion` | buy at z < −2 against the 20-day mean, sell at z > 0 |
| `atr_trailing_stop` | enter above SMA 50; exit 3 × ATR(22) below the highest close since entry |
| `sma_cross_long_short` | long while SMA 50 > SMA 200, short while below |

The statistics reported for each run:
- total return, CAGR, annualised volatility, Sharpe ratio (no risk-free rate)
  and maximum drawdown;
- trades, win rate and average trade return, net of costs;
- exposure, turnover and costs paid;
- the trade list, with fills.
