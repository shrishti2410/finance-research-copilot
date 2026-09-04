"""Stock price tool.

Intended contents:
- current quote and historical series for a ticker
- calls the market-data API (key/base URL from env)
- returns normalized {ticker, asof, price, currency, series?}
"""
