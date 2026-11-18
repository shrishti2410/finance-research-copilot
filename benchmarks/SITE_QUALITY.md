# Site quality: Lighthouse, axe and keyboard, before and after

Site quality, Tier 1, item 6 (the plan in CLAUDE.md). The targets were:
- Lighthouse ≥ 95 in all four categories for `/` and `/dashboard`;
- zero axe violations;
- keyboard navigation throughout;
- a text alternative for every chart.

**Result.** After the quality pass (`feat/quality-pass`):
- **100 in all four categories, for both pages, on the mobile and desktop
  presets, in all three final runs.**
- **0 axe violations** on `/`, `/dashboard` and the four stock-page tabs.
- **A clean keyboard walk-through.**

## Lighthouse

Lighthouse's default mobile preset (a mid-range phone, simulated throttling)
and its desktop preset. Each figure is one run; three runs a side, because
the mobile performance score moves between runs.

| page | preset | before: master `df30f35` (runs 1 / 2 / 3) | after: `feat/quality-pass`, final build (runs 1 / 2 / 3) |
|---|---|---|---|
| `/` | mobile | P **89 / 89 / 89**; A, BP, SEO 100. TBT 420 / 440 / 430 ms | **100** in all four, every run. TBT 0 / 40 / 20 ms |
| `/` | desktop | 100 in all four | 100 in all four |
| `/dashboard` | mobile | P **92 / 93 / 99**; A, BP, SEO 100. CLS 0.104 / 0 / 0 | **100** in all four, every run. CLS 0 |
| `/dashboard` | desktop | 100 in all four (CLS 0.057) | 100 in all four (CLS 0) |

P is performance, A accessibility, BP best practices.

What moved the scores:
- **`/` mobile, total blocking time 430 ms → ~20 ms.** The market strip is now
  rendered on the server into the cached page (`feat/cold-start`). Before,
  the browser fetched it after load and re-rendered the marquee inside the
  measured window. **Inferred** from the before trace, whose blocking time was
  spent in React's script chunk; not isolated by a separate experiment.
- **`/dashboard`, layout shift 0.104 → 0.** The loading skeleton now has the
  loaded page's shape: two markets, five and three cards at their measured
  202 px. The old skeleton was one index strip and five cards, so the footer
  jumped when the data arrived. Before, the shift depended on whether the
  data arrived inside the measurement window (0.104 in one run of three).

Two things seen along the way, kept here so the record is complete:
- **A build in between had SEO 63 on `/dashboard`.** My own `robots.txt`
  disallowed the signed-in pages, which Lighthouse reports as "page is
  blocked from indexing". `robots.txt` now allows everything but `/signup`.
  The three final runs above are on that fixed build.
- **That in-between build's first run scored 84 on `/` mobile (TBT 570 ms).**
  Its next two runs scored 100. The final build's three runs had no such
  outlier. Not explained: one cold first run, not seen again.

## axe-core 4.14.1

| page | before | after |
|---|---|---|
| `/` (1440 px, 390 px) | 0 | 0 |
| `/dashboard` (1440 px, 390 px) | **1 rule, 8 elements:** `label-content-name-mismatch` (serious). Each card's `aria-label`, "Coforge: open the stock page", did not contain its visible text. | 0. The label is gone, and each link is named by its content. |
| `/stock/SBIN.NS`: Chart, Signal, Backtest, Options (1280 px) | not run before | 0 |

On the stock page, the fixes were checked by re-running axe after each one:
- **Two violations my own first attempt introduced:**
  - `aria-hidden-focus`: the chart container held TradingView's attribution
    link;
  - `scrollable-region-focusable`: the new table's scroll box.
- **One pre-existing violation:** `empty-table-header` (minor) in the
  risk table.

## Keyboard

A script tabbed through each page (Tab only, up to 80 stops), recording each
stop's accessible name and whether its focus is visible:

| page | tab stops | without a name | focus not visible |
|---|---:|---:|---:|
| `/` | 25 distinct | 0 | 0 |
| `/dashboard` | 22 distinct | 0 | 0 |
| `/stock/SBIN.NS` (Chart tab) | 26 distinct | 0 | 0 |

- **No traps:** Tab always moved on.
- **Reachable:** the market strip's pause button, the tour tabs, the FAQ, the
  chart's indicator checkboxes and its data-table toggle.

## Charts

| chart | text alternative |
|---|---|
| price chart (canvas) | **new:** a screen-reader summary (range, first and last close, the change, latest indicator values) and a table of the last 20 days with every plotted series, behind a keyboard-reachable toggle |
| equity curve, option payoff, sparklines (SVG) | already had `role="img"` with a summary label |

## How it was run

| | |
|---|---|
| tools | Lighthouse 12.8.2 (Node API) and axe-core 4.14.1 in headless Microsoft Edge 157.0.4341.60, run by puppeteer-core 25.16.0. The scripts are in the session's scratchpad, not the repository. |
| target | local production builds (`next build` + `next start`) against the local API and pgvector database; `/dashboard` signed in with a throwaway local account |
| before | master `df30f35`, three runs, 19:52–20:01 UTC on 2026-11-17 |
| after | `feat/quality-pass` at its final build (`6dcb720`'s code), three runs, 20:09–20:16 UTC; axe on the stock tabs re-run after the Backtest fix |
| machine | Windows 11 laptop, plugged in, standby not entering during the runs: the System log shows no sleep event between 19:30 and 21:32 UTC |

**What these numbers are not.** These are local builds on a laptop.
Vercel's edge servers, a real phone and a real network will score
differently. A run against the live site, after these branches are
deployed, is the check that counts. Not run yet.
