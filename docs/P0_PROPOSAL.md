# P0 proposal: data model, endpoints and data sources

Status: **proposed 2026-11-18, awaiting approval.** Nothing in P0 is built
until this is approved (CLAUDE.md, "The product plan").

Covers search, the company page and compare (P0.1–P0.3), plus what the
watchlist and freshness labels (P0.4–P0.5) need from the API. Every figure
below is marked **measured** (a command run on 2026-11-18, raw output in
`benchmarks/results/2026-11-18-product-p0/`), **read** (a published page,
quoted, with its URL), **reported** (users' reports, not official) or
**design** (a number chosen here, not measured).

## 1. Data sources and their limits

### NSE listed-equity list (search universe, India)

- **The file** (measured): `https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv`
  returned HTTP 200 to a plain `curl`: 185,917 bytes, 2,632 rows (2,404 in
  series EQ, 194 BE, 34 BZ). Its columns: `SYMBOL, NAME OF COMPANY, SERIES,
  DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE`.
- **There is no sector column.** Sector and industry come from Yahoo per
  company (below), or from an NSE index list with an industry column. That
  list's terms have not been checked.
- **The licence has not been checked.** This needs your check before a
  snapshot is committed: `www.nseindia.com` returns 403 to non-browser
  clients, and `/terms-of-use` redirects to a 404 (measured). A web-fetch
  attempt timed out on every NSE page. So whether a dated copy may sit in a
  public repo is unknown. If the terms forbid it, keep the snapshot out of
  git and load it at deploy time; that is the fallback design.

### US universe

- **SEC's `company_tickers_exchange.json`** (ticker, name, CIK, exchange),
  under the same SEC fair-access policy the project already follows for
  EDGAR (a declared User-Agent, at most 10 requests a second). **Not
  re-read today:** confirm the file and the policy before relying on it.
- **10-K research stays the 15 indexed companies.** Other US stocks get
  Yahoo fundamentals, labelled as Yahoo, as NSE stocks do.

### Yahoo Finance (prices, fundamentals, news, sector), via yfinance

- **The terms are the main risk in P0, and today's site has it already.**
  - Yahoo's terms (read,
    https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html, section 2)
    bar collecting data "using any automated means … for any purpose
    without our express, prior permission".
  - They also bar creating a "database, archive … data feed" that
    "constitutes a material substitute for the Services".
  - The help page (read, https://help.yahoo.com/kb/SLN2310.html) says "You
    must not redistribute information displayed on or provided by Yahoo
    Finance".
  - yfinance's README (read) says the API "is intended for personal use
    only".
  - The live site already uses Yahoo for every price. P0 would widen that
    to fundamentals and news for about 2,600 more stocks. **Your decision;
    see section 5.**
- **Rate limits.** Yahoo publishes none (read). Users report (not official):
  - about 100 requests, then a 30-second pause;
  - IP blocks lasting from an hour to more than a day;
  - 429s on the first `quoteSummary` call (yfinance discussions #2431 and
    #2581, 2025–2026).
  - So every Yahoo call is cached server-side and throttled globally
    (section 3).
- **Delays** (read, the SLN2310 table):
  - NSE (`.NS`): "Real-time".
  - Nasdaq: "Real-time".
  - NYSE-listed stocks: not stated. Only "NYSE Indices", at 15 min.
  - The freshness labels therefore say what we know: the source's stated
    delay, and when we fetched.

### On-demand signal reports

- **Measured on this laptop** (master `e16826d`): one stock's full report,
  computed from cached prices with the precompute bypassed, took 1.45–1.53 s
  of CPU (SBIN.NS and NVDA, two runs each).
- **Inferred, not measured:** at Render's 0.1 CPU that is about 15 s per
  report, longer on a slower core. Two at once would each take about 30 s.
- **Design:**
  - one on-demand report at a time, in a Redis-locked queue;
  - a "computing" state on the page, cached for the market day;
  - the watchlist stays precomputed;
  - a cap of about 20 on-demand reports per hour.
  - Before shipping, measure in the 0.1-CPU container the earlier load test
    used (`benchmarks/LOAD_TEST_MARKET.md`).

## 2. Data model (Postgres; one migration)

```
instruments                       -- the search universe, from the dated snapshots
  id            serial pk
  exchange      text   -- NSE | NASDAQ | NYSE | ...
  symbol        text   -- SBIN, NVDA
  yahoo_symbol  text   -- SBIN.NS, NVDA
  name          text
  isin          text null          -- NSE
  cik           int  null          -- US (SEC)
  series        text null          -- NSE EQ/BE/BZ
  sector        text null          -- filled lazily from Yahoo, labelled
  industry      text null
  has_10k       bool               -- one of the 15 indexed companies
  in_watchlist  bool               -- precomputed signals
  source        text               -- "NSE EQUITY_L 2026-11-18", "SEC company_tickers_exchange 2026-11-18"
  unique (exchange, symbol)
  index: pg_trgm GIN on name and symbol (to confirm Neon has pg_trgm)

company_data                      -- cached Yahoo / XBRL payloads, one row per kind
  instrument_id fk
  kind          text   -- profile | annual | quarterly | ratios | news
  payload       jsonb  -- with units declared per field (as for the agent tools)
  source        text   -- "SEC XBRL" | "Yahoo Finance"
  basis         text null -- consolidated | standalone | not stated by source
  fiscal_year_end text null
  currency      text
  fetched_at    timestamptz
  expires_at    timestamptz        -- profile/annual 7 d, quarterly 1 d, news 1 h (design)
  primary key (instrument_id, kind)

signal_reports                    -- the existing precompute table, extended
  + status      text   -- ready | computing | failed
  + origin      text   -- precompute | on_demand
```

- **The watchlist (P0.4)** is in the browser's localStorage only: lists,
  order and notes. No table and no signup.
- **Freshness (P0.5)** is a block on every price and figure group:
  `{type: previous_close|delayed|live|reported, as_of, exchange_tz,
  market_open, source}`.

## 3. Endpoints (public, per-IP rate limits as today)

| Endpoint | Returns | Notes |
|---|---|---|
| `GET /search?q=&limit=10` | symbol, exchange, name, sector, `has_10k`, `in_watchlist` | DB only, never Yahoo; name, symbol, ISIN or sector |
| `GET /company/{ticker}` | overview: price (+freshness), market cap, sector, description | profile from cache, else one Yahoo call |
| `GET /company/{ticker}/financials?period=annual\|quarterly` | revenue, profit, EPS, cash flow, by period | XBRL for the 15; Yahoo, labelled, otherwise |
| `GET /company/{ticker}/ratios` | P/E, P/B, ROE, debt/equity, margins, growth | computed in code from the cached statements and price; units declared |
| `GET /company/{ticker}/news` | headlines with source and time | by full company name |
| `GET /company/{ticker}/audit` | signal, backtest, options, paper record | precomputed for the watchlist; else 202 `{status: computing}`, then poll |
| `GET /compare?tickers=A,B[,C,D]&period=annual` | aligned table + flags | flags: fiscal year-ends differ, consolidated vs standalone, currency |
| `POST /ask` (existing) | answer + evidence | the company page passes the ticker as context; the resolver is shared |

- **One resolver** (`instruments`) serves `/search`, every route above and
  every agent tool.
- **Yahoo calls** go through one server-side gate:
  - a Redis single-flight lock per (ticker, kind);
  - a global cap of about 30 calls a minute (design);
  - on a 429, back off and serve the stale cache, labelled with its date.

## 4. Order of work (each its own branch, tests first)

1. **`instruments`, the snapshots, the resolver and `/search`.** This
   includes NSE Level A: agent tools use the resolver, and the prompt
   routes NSE questions.
2. **The `/company/{ticker}` read endpoints and the cache gate,** then the
   page.
3. **On-demand audit:** the queue and the computing state, measured at 0.1
   CPU before shipping.
4. **`/compare`,** then the page.
5. **The watchlist (localStorage)** and the freshness labels everywhere.

## 5. Decisions needed

1. **NSE list licence.** Please open NSE's terms in a browser (from the
   footer of nseindia.com) and say whether a dated copy may go in the
   public repo. If not, the fallback is to load it at deploy time and keep
   it out of git.
2. **Yahoo terms.** Automated access and redistribution are barred without
   permission, and the site already depends on Yahoo for prices. The
   options:
   - (a) continue as an educational project, labelling the source and
     caching hard (P0 adds fundamentals and news for every stock);
   - (b) keep Yahoo to the watchlist only, and limit the company page for
     other stocks to what has a clearer licence (the SEC data for US
     stocks; for NSE stocks only the list itself, with no prices);
   - (c) look for a licensed free source first.

   I have not found or checked a licensed free source for NSE
   fundamentals.
3. **The on-demand audit cap** (about 20 reports an hour, one at a time),
   until it is measured at 0.1 CPU.
