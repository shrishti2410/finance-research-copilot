# Structured financial facts

Reported figures by company, concept and fiscal year, from SEC's XBRL
company-facts API, stored in Postgres and read by the agent's
`get_financial_fact` tool.

## Why

A question such as "What was NVIDIA's diluted EPS in fiscal 2024?" used to be
answered by retrieving the income statement and reading one cell of a rendered
grid. The eval runs of 2026-11-10 to 2026-11-15 show what goes wrong on that
path: the wrong year's column, the wrong row of two with the same label, or
the latest-period ratio tool called for a question that names an earlier year
(`benchmarks/RUN_C_ANALYSIS.md`, `benchmarks/MODEL_COMPARISON.md`). The same
question is now a lookup keyed by company, concept and fiscal year, and the
answer comes back with the accession number of the filing it was taken from.

## The pieces

| piece | where |
|---|---|
| fetch | `EdgarClient.company_facts(ticker)` in `ingestion/edgar_client.py` |
| parse | `ingestion/xbrl_facts.py` (`annual_facts`) |
| table | `xbrl_facts`, model `db.models.XbrlFact`, migration `0003_xbrl_facts` |
| store and read | `tools/facts.py` (`upsert_facts`, `get_financial_fact`, `ratio_for_fiscal_year`) |
| index | `python scripts/index_facts.py` (NVDA and AAPL by default) |
| agent | `agent/tools.py` registers `get_financial_fact` first and gives `calculate_ratio` a `fiscal_year` |

**Fetch.** One request per company to
`https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`. It goes
through `EdgarClient._get`, so the SEC User-Agent check, the rate limiter
(`SEC_REQUESTS_PER_SECOND`, under SEC's stated maximum of 10 a second) and the
retry policy are the ones every other EDGAR call uses. The JSON is cached
under `EDGAR_CACHE_DIR/<TICKER>/companyfacts.json`.

**Parse.** The document holds every tagged number the company has filed. Three
properties of it decide the parsing, and each has a test in
`tests/test_facts.py`:

- `fy` on an entry is the fiscal year of the *filing*, not of the number. A
  10-K prints three years and all three arrive with the same `fy`. The fiscal
  year of a number is taken from its period end instead: the smallest `fy`
  among annual reports that printed a full-year figure ending on that date.
- The same number is reported again by each later 10-K, sometimes restated.
  One row is kept per fiscal year, the most recently filed, with the
  accession number of the filing it came from. NVIDIA's fiscal 2024 diluted
  EPS is stored as $1.19, the figure every report since the 2024 split
  gives, and not the $11.93 originally filed.
- Only annual figures are kept: durations of 350 to 380 days and balances
  dated on a fiscal year end, from 10-K, 20-F and 40-F reports and their
  amendments. Quarterly figures are not ingested. The `fiscal_period` column
  exists so they can be added without a migration.

**Table.** `xbrl_facts` is unique on (cik, taxonomy, concept, unit,
fiscal_year, fiscal_period), so indexing a company again replaces its rows.
`value` is `NUMERIC(30, 6)`. See `docs/DATABASE.md` for the column list.

**The pin.** NVDA and AAPL are the eval companies, and their facts are taken
*as filed through* `PINNED_FACTS_THROUGH` in `ingestion/pipeline.py`. The
company-facts document is not one filing: it grows with each report, and a
later report can restate an earlier year. Without a pin the figures the eval
sees would move the next time either company files, which is what happened to
the filing index before it was pinned by accession number. Other companies are
not pinned. `--latest` ignores the pin.

## The tool

`get_financial_fact(ticker, fact, fiscal_year)` takes a short fact name, not an
XBRL concept. `FACTS` in `tools/facts.py` maps each name to the concepts that
mean it, in order of preference, because companies change tags over time:
revenue is `Revenues` for NVIDIA since fiscal 2023 and
`RevenueFromContractWithCustomerExcludingAssessedTax` for Apple. The result
names the concept that was used.

A result carries `value` and `unit` (dollars, shares, or dollars per share;
never millions), `period_start` and `period_end`, `concept`, and the citation:
`accession`, `form`, `filed` and `source_url`.

Three figures that exist in the data are deliberately not named facts:
dividends per share, dividends paid and share repurchases. On the fast subset
of 2026-11-15 a question about Apple's *quarterly* dividend was answered with
the year's total, and a question about NVIDIA's repurchase *authorization* with
the cash spent. Describing the facts as "not the quarterly rate" did not stop
it. Asking for one is now an unknown fact that points to `search_filings`.

The tool also rejects an argument it does not have. A call with
`segment="Data Center"` used to have the segment dropped and return total
revenue; it is now a `bad_input` error saying there is no segment figure here.
Every result carries a `scope` line saying it covers the whole company for the
whole fiscal year.

Failures are the same envelope every tool uses. An unknown fact name lists the
known ones. A year the company has not reported lists the years it has. A
company that has not been ingested says so and points at the other tools.

`calculate_ratio(..., fiscal_year=2025)` computes gross, operating and net
margin, return on equity and current ratio from that year's facts, with the
same formulas as the latest-period path and the accession numbers of its
inputs. Two ratios do not have a fiscal-year form from facts:

- `debt_to_equity`: total debt is Yahoo's definition and no single XBRL
  concept matches it. With a fiscal year it is answered from the
  latest-period source when that year is the one the source covers, and is
  otherwise an `unsupported` error that names `long_term_debt` and
  `shareholders_equity`.
- `pe_ratio`: it needs a share price. The error says to divide a close from
  `get_stock_price` by `eps_diluted`.

## What is not in company-facts

Company-facts holds only numbers reported for the whole entity. A figure with
a dimension, such as Apple's Services net sales or NVIDIA's Data Center
revenue, is not in it. Those stay with the filing text.

For the one case where the statement itself breaks the top line down, the
income statement chunk's caption now spells the categories out:
"Net sales by category for the periods ending ...: Products net sales ...;
Services net sales ...". Apple's statement has a row labelled "Services" under
net sales and another under cost of sales, and the model had read the second
as the first. This changes the text of the statement chunk, so the filings
have to be indexed again (`python scripts/index_filings.py`).

## Routing

The system prompt and the tool descriptions send a question about a
company-wide figure for a named fiscal year to `get_financial_fact` or
`calculate_ratio(fiscal_year=...)` first, and keep `search_filings` for
wording, segment figures and anything those two do not hold. The facts tool is
registered first. `eval/questions.json` is unchanged; its `answerable_via`
labels still name the tools the questions were written against.

## edgartools

The brief was to evaluate [edgartools](https://github.com/dgunning/edgartools),
check its licence, and use it if it beats the existing lxml code. It is not
used. This is an assessment from its published metadata on 2026-11-15; the
library was **not installed or run**.

| | |
|---|---|
| licence | MIT, per `pyproject.toml`, the README and PyPI's `license_expression`. The licence file itself (`LICENSE.txt`) was not read: the fetch of `LICENSE` returned 404. |
| version | 6.2.0, uploaded to PyPI on 2026-11-15, after 6.1.0 on 2026-11-12 and 6.0.0 on 2026-11-09 |
| Python | 3.11 or later |
| runtime dependencies | 18, including pandas, pyarrow 23 or later, lxml, pydantic, rich, and `httpx2` in place of `httpx` |

Why not, for this job:

- **There is no lxml code for it to beat here.** The lxml parser reads the
  10-K's HTML for the retrieval index, and that stays. Company-facts is JSON:
  the fetch is one call on the existing client and the parsing is
  `ingestion/xbrl_facts.py`, about a hundred lines with no new dependency.
- **It would bypass this project's EDGAR rules.** It sets its own identity,
  rate limiter and cache. This project validates the SEC User-Agent at
  startup, shares one rate limiter across every EDGAR call and retries through
  `core/retry.py`. A second HTTP stack would sit outside all three.
- **Its dependency weight is out of proportion.** pandas and pyarrow are not
  otherwise needed by the ingestion path, and `httpx2` would sit beside the
  `httpx` the whole app uses.
- **Its release was hours old.** Three releases in a week means a pin would be
  needed, and a version uploaded the same day is not one to add to a build.

Where it could still earn a place: segment and other dimensional figures live
in each filing's XBRL instance, not in company-facts, and a library that reads
the instance could supply them. Its README does not say whether it exposes
them, and this was not tested. That is the question to answer before
revisiting it.

## Running it

```bash
alembic upgrade head              # creates xbrl_facts (migration 0003)
python scripts/index_facts.py     # NVDA and AAPL, as filed through the pinned date
python scripts/index_filings.py   # re-index, for the new statement caption
```
