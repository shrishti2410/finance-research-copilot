# Strategy cards

One card per strategy: what the signal page explains a signal with, and what
the agent cites. The engine computes every number. A card explains the
reasoning behind a rule and where it breaks. It never gives a price or a
forecast.

Written for this project in its own words; no text is copied from a book or
site.

## Format

Front matter, then seven sections, all required (checked by
`tests/test_strategy_cards.py` and `python scripts/index_strategies.py --check`):

    ---
    key: sma_cross                     # = the file name, and for equity cards
    name: Moving-average crossover     #   the key in market/strategies.py
    kind: equity                       # equity | option
    regime: trending                   # any | trending | ranging | downtrend | volatile
    view: bullish                      # options only: the market view
    code: market/strategies.py#sma_cross    # or "pending"
    ---
    ## Summary / ## Rules / ## Regime / ## When it fails / ## Risk / ## Parameters / ## Code

## The cards

- **18 equity cards:** one for each strategy in `market/strategies.py`.
- **8 options cards:** long call, long put, covered call, protective put,
  bull call spread, bear put spread, long straddle and iron condor. Their code
  is pending until step 7C phase 5 adds payoffs and premium estimates.

## The index

`python scripts/index_strategies.py` embeds each section, with the card's
name and an option's view, using bge-small. It replaces the `strategy_cards`
table (migration 0007). That table is a separate collection from
`filing_chunks`: a strategy search never returns a filing passage, and the
reverse. On 2026-11-16 the run indexed 26 cards as 182 sections in 28 s on
this laptop's CPU, including the model load.

Retrieval was checked with six questions (`tests/test_strategy_cards.py`,
PROBES), measured on the same day:
- five rank the expected card first;
- "profit from a big move after results without knowing the direction" ranks
  `long_straddle` second, behind `rsi2_pullback`.

That is a smoke test, not a retrieval benchmark.
