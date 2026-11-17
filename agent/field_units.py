"""The unit of every numeric field each agent tool returns.

The grounding guard matches a figure written as a percentage only against
percentages and ratios x 100 (agent/grounding.py, `percent_evidence`). It needs
to know which fields are which. A field named `*_pct` says so itself; for the
rest, this table says. The audit behind it (2026-11-17) ran every tool on real
data: no tool returns a percentage in a field whose name lacks pct/percent.
Rates of return, volatility and drawdowns are fractions (-0.319 for a 31.9%
drawdown), and levels such as an ADX of 44.8 or an RSI of 72.3 are not
percentages at all.

tests/test_tool_units.py fails when a tool returns a numeric field that is not
declared here, so a new field cannot slip in with an unknown unit. The captured
outputs it checks are in tests/fixtures/tool_outputs.json.

Units:
    percent      already a percentage: 13.2 means 13.2%
    ratio        a fraction: 0.132 means 13.2%
    unit_field   the sibling "unit" says: "percent" -> percent, otherwise ratio
    fact         an XBRL fact: sibling unit "pure" -> ratio, any other unit -> never a percentage
    level        a number on its own scale, never a percentage (Sharpe, beta, ADX, RSI, score)
    count, money, date   never a percentage
"""

from __future__ import annotations

PERCENT_UNITS = ("percent",)

# tool -> field path -> unit. Paths use "." for nesting and "[]" for list items.
TOOL_FIELD_UNITS: dict[str, dict[str, str]] = {
    "get_financial_fact": {"fiscal_year": "date", "value": "fact"},
    # value: a margin or return as a fraction (0.7107), or a multiple (a P/E of
    # 30.5, a current ratio of 1.2). Either way x 100 is the only percentage it
    # can support. Every inputs.* field is a statement amount or a price:
    # see unit_of.
    "calculate_ratio": {"value": "ratio"},
    "calculate": {"value": "unit_field", "inputs.a.value": "money", "inputs.b.value": "money"},
    "get_stock_price": {
        "trading_days": "count", "change": "money", "change_pct": "percent",
        "first.close": "money", "last.close": "money", "high.close": "money", "low.close": "money",
        "prices[].open": "money", "prices[].high": "money", "prices[].low": "money",
        "prices[].close": "money", "prices[].volume": "count",
    },
    "get_price_analytics": {
        "trading_days": "count", "overlapping_return_days": "count",
        "total_return": "ratio", "annualised_return": "ratio", "annualised_volatility": "ratio",
        "best_day": "ratio", "worst_day": "ratio", "max_drawdown": "ratio", "risk_free_rate": "ratio",
        "sharpe_ratio": "level", "beta": "level", "correlation": "level",
    },
    "get_trading_signal": {
        # confidence is a 0-1 share, capped at the coverage fraction: "44% confidence"
        # is a fair way to state 0.44. The score (-1 to +1) is not a share.
        "score": "level", "confidence": "ratio",
        "regime.adx": "level", "regime.plus_di": "level", "regime.minus_di": "level",
        "votes[].share_of_vote": "ratio",
    },
    "search_filings": {"count": "count", "results[].score": "level"},
    "show_price_chart": {"bars": "count", "last_close": "money", "latest.rsi14": "level"},
}


def unit_of(tool: str | None, path: str) -> str | None:
    """The declared unit of a field, or None when the tool or field is not
    declared (the guard then treats it as before: a possible ratio)."""
    if tool is None:
        return None
    fields = TOOL_FIELD_UNITS.get(tool)
    if fields is None:
        return None
    if path in fields:
        return fields[path]
    # calculate_ratio's operands: gross_profit, revenue, total_debt, equity,
    # current_assets, price, eps, ... (tools/ratios.py, tools/facts.py).
    if tool == "calculate_ratio" and path.startswith("inputs."):
        return "money"
    # Any latest indicator value on a chart is a level (rsi14, sma50, macd, ...).
    if tool == "show_price_chart" and path.startswith("latest."):
        return "level"
    return None
