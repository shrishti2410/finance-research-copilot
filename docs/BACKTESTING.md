# Strategies and backtesting (version 2, step 7B)

`market/strategies.py` holds 18 strategies behind one interface: classics
including buy-and-hold as the benchmark, and a long/short variant of the
crossover. The first 13 came in step 7B; step 7C added the last five. Each
one has a card in `docs/strategies/` (see its README). `market/backtest.py` runs any of them on daily bars with
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
- **Costs.** Each leg of a trade is priced by the ticker's market's cost
  model (`market/costs.py`; see "Indian stocks" below). For a US ticker that
  is `cost_bps` of the traded notional on each side. Slippage
  (`slippage_bps`) is charged on top, on the whole traded notional. Going
  from long to short trades twice the equity. A recorded fill is the open,
  moved against the trader by the slippage. A trade's net return is measured
  on the cash committed to it, entry fees included.
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
| `rsi2_pullback` | buy RSI(2) < 10 above SMA 200, sell above SMA 5 |
| `keltner_breakout` | buy above EMA 20 + 2 ATR(20), sell below EMA 20 |
| `adx_trend` | long while ADX(14) > 25 and +DI > -DI |
| `trend_alignment` | long while close > SMA 20 > SMA 50 > SMA 200 |
| `obv_trend` | long while OBV and the close are above their 50-day SMAs |

The statistics reported for each run:
- total return, CAGR, annualised volatility, Sharpe ratio (no risk-free rate)
  and maximum drawdown;
- trades, win rate and average trade return, net of costs;
- exposure, turnover and costs paid;
- the trade list, with fills.

## Indian stocks (version 2, step 7C)

An NSE ticker is written with Yahoo's `.NS` suffix (`SBIN.NS`), a BSE one with
`.BO`, and the Nifty 50 index is `^NSEI`. `market/markets.py` maps each ticker
to its exchange, currency and cost model:

| ticker | exchange | currency | costs | shorts | default benchmark |
|---|---|---|---|---|---|
| `NVDA`, `BRK.B` | US | USD ($) | `cost_bps` per side | allowed | SPY |
| `SBIN.NS`, `^NSEI` | NSE | INR (₹) | Indian equity delivery | **no**: long or flat | ^NSEI |
| `RELIANCE.BO` | BSE | INR (₹) | Indian equity delivery | no | ^BSESN |

Chart, analytics and backtest results carry `exchange`, `currency` and
`currency_symbol`. A backtest also carries `costs`, the model and its rates.

### The Indian cost model

Equity delivery (bought, held overnight, sold later), for an individual, in
`market.costs.IndiaEquityDelivery`:

| charge | rate | charged on | side |
|---|---|---|---|
| brokerage | ₹0 | - | both |
| STT | 0.1% | turnover | buy and sell |
| NSE transaction charge | 0.00307% | turnover | both |
| SEBI turnover fee | ₹10 per crore | turnover | both |
| stamp duty | 0.015% | turnover | buy |
| GST | 18% | brokerage + exchange charge + SEBI fee | both |
| DP charge | ₹15.34, incl. GST, per stock per sell day | flat | sell |

**Source:** the equity-delivery column of Zerodha's charges page,
https://zerodha.com/charges, read on 2026-11-16. That page is one broker's
summary of statutory and exchange charges. When the primary sources were
looked up the same day, NSE's page timed out and the Income Tax department's
page returned 404, so **these rates are not confirmed from an exchange or a
government source**. Each one is a constructor parameter.

Worked example, ₹1,00,000 bought and sold (tested in
`tests/test_markets_costs.py`):
- buy: STT ₹100 + exchange ₹3.07 + SEBI ₹0.10 + stamp ₹15 + GST ₹0.57 = **₹118.74**;
- sell: STT ₹100 + exchange ₹3.07 + SEBI ₹0.10 + GST ₹0.57 + DP ₹15.34 = **₹119.08**.

The round trip costs about 0.24%, against 0.10% under the old flat 5 bps per
side. STT dominates.

**No overnight shorts.** A delivery position cannot be held short overnight in
the cash segment; shorting in India goes through futures, which this engine
does not model. So the Indian model clips a short signal to flat, and
`sma_cross_long_short` on an NSE stock is long or flat.

### The watchlist and its data

`market/universe.py` lists COFORGE.NS, DIXON.NS, SBIN.NS, PREMIERENE.NS and
HDFCBANK.NS, benchmarked against ^NSEI. `python scripts/load_prices.py` loads
five years plus the 400-day warm-up into the price cache. It reports each
ticker's rows, first and last day and largest gap between bars, and fails on
a gap over seven days.

Run on 2026-11-16 against the local pgvector database:

```
ticker         ccy    rows  first      last        gap  note
COFORGE.NS     INR    1508  2020-10-12 2026-11-16    5
DIXON.NS       INR    1508  2020-10-12 2026-11-16    5
SBIN.NS        INR    1508  2020-10-12 2026-11-16    5
PREMIERENE.NS  INR     546  2024-09-03 2026-11-16    4  limited history
HDFCBANK.NS    INR    1508  2020-10-12 2026-11-16    5
^NSEI          INR    1506  2020-10-12 2026-11-16    5
```

- **Coforge** was NIIT Technologies until 2020. Yahoo's COFORGE.NS series is
  continuous back to 2004 under the one symbol: no gap over a week. Its two
  splits (2007, and 2025-06-04) are absorbed by the adjusted close.
- **Premier Energies** listed on 2024-09-03, so it has 546 trading days, about
  2.2 years. `market.universe.history_of` marks any stock with fewer than 750
  trading days as limited history. The signal engine shortens its windows for
  such a stock, and lowers its confidence (step 7C, phase 3).
