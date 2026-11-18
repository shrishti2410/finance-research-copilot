"""Load test of the endpoints that never call a model (plan item 4).

    locust -f benchmarks/locustfile_market.py --host http://127.0.0.1:8100 \
        --headless -u 10 -r 10 -t 5m --csv <out>/u10

What a visitor to the deployed site does, without the research chat: the
landing page's market strip, the dashboard, and the stock pages' signal,
options and chart tabs, plus the session check. Each simulated user signs in
with one of the tokens in LOAD_TOKENS_FILE (one per line, made beforehand
with throwaway local accounts, so the bcrypt cost of signing up is not part of
what is measured). Think time 1-5 s between requests.

Run it only against a local container, never the live site.
"""

from __future__ import annotations

import itertools
import os
import random
from datetime import date, timedelta

from locust import HttpUser, between, task

TOKENS = [t.strip() for t in open(os.environ["LOAD_TOKENS_FILE"], encoding="utf-8") if t.strip()]
_next = itertools.cycle(TOKENS)
TICKERS = ["COFORGE.NS", "DIXON.NS", "SBIN.NS", "PREMIERENE.NS", "HDFCBANK.NS", "NVDA", "AAPL", "MSFT"]
END = date.today()
START = END - timedelta(days=730)


class Visitor(HttpUser):
    wait_time = between(1, 5)

    def on_start(self):
        self.client.headers["Authorization"] = f"Bearer {next(_next)}"

    @task(3)
    def market_strip(self):
        self.client.get("/market/strip", name="/market/strip")

    @task(3)
    def watchlist(self):
        self.client.get("/market/watchlist", name="/market/watchlist")

    @task(4)
    def signal_report(self):
        self.client.get(f"/market/{random.choice(TICKERS)}/signal/report", name="/market/[t]/signal/report")

    @task(2)
    def options(self):
        self.client.get(f"/market/{random.choice(TICKERS)}/options", name="/market/[t]/options")

    @task(2)
    def chart(self):
        self.client.get(f"/market/{random.choice(TICKERS)}/chart",
                        params={"start": START.isoformat(), "end": END.isoformat(),
                                "indicators": "volume,sma50,sma200,rsi14,macd"},
                        name="/market/[t]/chart")

    @task(1)
    def me(self):
        self.client.get("/auth/me", name="/auth/me")

    @task(1)
    def health(self):
        self.client.get("/health", name="/health")
