"""Chart data, price analytics, the two agent tools, and the chart an answer
carries. Live Postgres for the cache; a fake provider, so nothing reaches
Yahoo Finance.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from market import service
from market.providers import Bar
from market.store import MarketData

TICKERS = ("ZZCHART", "ZZBENCH")


class Fake:
    name = "fake"

    def daily_bars(self, ticker, start, end):
        out, day, i = [], start, 0
        while day <= end:
            if day.weekday() < 5:
                base = 100 + 10 * math.sin(i / 9) + i * 0.05 + (5 if ticker == "ZZCHART" else 0)
                out.append(Bar(day=day, open=base, high=base + 1, low=base - 1, close=base,
                               adj_close=base * 0.99, volume=1_000_000 + i))
                i += 1
            day += timedelta(days=1)
        return out


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def fake_market(monkeypatch):
    from db.base import engine

    async def wipe():
        await engine.dispose()
        async with engine.begin() as conn:
            for t in TICKERS:
                await conn.execute(text("DELETE FROM price_bars WHERE ticker = :t"), {"t": t})
                await conn.execute(text("DELETE FROM price_fetches WHERE ticker = :t"), {"t": t})
        await engine.dispose()

    try:
        run(wipe())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"price tables not available ({type(exc).__name__}); migrate to 0006")
    monkeypatch.setattr(service, "MarketData", lambda: MarketData(Fake()))
    yield
    run(wipe())


def call(coro):
    from db.base import engine

    async def go():
        try:
            return await coro
        finally:
            await engine.dispose()
    return run(go())


START, END = date(2026, 3, 2), date(2026, 6, 30)


def test_chart_data_shows_only_the_range_but_warms_the_indicators_up(fake_market):
    data = call(service.chart_data("zzchart", START, END, ["sma200", "rsi14", "bollinger"]))
    assert data["ticker"] == "ZZCHART" and data["provider"] == "fake"
    assert data["bars"][0]["time"] >= START.isoformat()
    assert data["bars"][-1]["time"] <= END.isoformat()
    # Warm-up fetched before START, so even SMA 200 has a value on the first day shown.
    assert data["series"]["sma200"][0]["time"] == data["bars"][0]["time"]
    assert set(data["series"]) == {"sma200", "rsi14", "bb_middle", "bb_upper", "bb_lower"}
    assert all(len(points) == len(data["bars"]) for points in data["series"].values())


def test_unknown_or_too_many_indicators_are_refused():
    with pytest.raises(ValueError, match="Known"):
        service.parse_indicators("sma20,stochastic")
    with pytest.raises(ValueError, match="At most"):
        service.parse_indicators(list(service.indicators.INDICATORS)[:9])
    assert service.parse_indicators(" SMA20 , rsi14,sma20") == ["sma20", "rsi14"]


def test_price_analytics_has_beta_against_the_benchmark(fake_market):
    out = call(service.price_analytics("ZZCHART", START, END, "ZZBENCH"))
    assert out["benchmark"] == "ZZBENCH" and out["beta"] is not None
    assert out["price_basis"] == "dividend-adjusted close"
    assert out["max_drawdown"] < 0


def test_the_chart_tool_returns_a_chart_and_the_latest_values(fake_market):
    from agent.tools import show_price_chart
    out = call(show_price_chart("ZZCHART", START.isoformat(), END.isoformat(), ["sma50", "macd"]))
    assert out["ok"] and out["chart"] == {"kind": "price_chart", "ticker": "ZZCHART",
                                          "start": START.isoformat(), "end": END.isoformat(),
                                          "indicators": ["sma50", "macd"]}
    assert set(out["latest"]) == {"sma50", "macd", "macd_signal", "macd_hist"}


@pytest.mark.parametrize("args, code", [
    (("ZZCHART", "2026-13-01"), "bad_input"),
    (("ZZCHART", "2026-06-01", "2026-05-01"), "bad_input"),
    (("ZZCHART", "2026-03-02", "2026-06-30", ["stochastic"]), "bad_input"),
])
def test_the_chart_tool_explains_bad_arguments(fake_market, args, code):
    from agent.tools import show_price_chart
    out = call(show_price_chart(*args))
    assert out["ok"] is False and out["error"] == code


def test_the_analytics_tool_returns_the_figures(fake_market):
    from agent.tools import get_price_analytics
    out = call(get_price_analytics("ZZCHART", START.isoformat(), END.isoformat(), "ZZBENCH"))
    assert out["ok"] and out["trading_days"] > 60 and out["beta"] is not None


def test_an_answer_carries_only_the_charts_that_were_drawn():
    from agent.orchestrator import AgentResult, Step

    def step(tool, ok, result):
        return Step(iteration=1, tool=tool, arguments={}, result=result, latency_ms=1,
                    model_latency_ms=1, ok=ok)
    chart = {"kind": "price_chart", "ticker": "NVDA", "start": "2026-01-01",
             "end": "2026-06-30", "indicators": []}
    result = AgentResult(answer="x", steps=[
        step("show_price_chart", True, {"ok": True, "chart": chart}),
        step("show_price_chart", False, {"ok": False, "error": "bad_input"}),
        step("get_stock_price", True, {"ok": True, "chart": "not one"}),
    ])
    assert result.charts == [chart]


def test_the_chart_endpoint_needs_a_login_and_serves_bars(fake_market, monkeypatch):
    from api.main import app
    from core.config import settings

    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    with TestClient(app) as client:
        if client.get("/health/db").status_code != 200:
            pytest.skip("Postgres unreachable")
        url = f"/market/ZZCHART/chart?start={START}&end={END}&indicators=sma20,volume"
        assert client.get(url).status_code == 401
        token = client.post("/auth/signup", json={
            "email": f"chart-{uuid.uuid4().hex[:10]}@example.com",
            "password": "correct-horse-battery"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        body = client.get(url, headers=headers).json()
        assert body["bars"] and {"sma20", "volume", "volume_sma20"} <= set(body["series"])
        bad = client.get(f"/market/ZZCHART/chart?indicators=nope", headers=headers)
        assert bad.status_code == 422
        stats = client.get(f"/market/ZZCHART/analytics?start={START}&end={END}"
                           f"&benchmark=ZZBENCH", headers=headers).json()
        assert stats["benchmark"] == "ZZBENCH"


def test_backtest_service_trades_the_window_with_a_warm_up(fake_market):
    out = call(service.backtest("ZZCHART", "sma_cross", START, END, {"fast": 10, "slow": 30},
                                cost_bps=2, slippage_bps=1))
    assert out["start"] >= START.isoformat() and out["end"] <= END.isoformat()
    assert out["params"] == {"fast": 10.0, "slow": 30.0}
    assert len(out["equity"]) == len(out["days"]) == len(out["benchmark_equity"])
    assert "next open" in out["execution"]
    assert all(set(t) >= {"side", "entry_day", "entry_price", "net_return"} for t in out["trades"])


def test_the_backtest_endpoints(fake_market, monkeypatch):
    from api.main import app
    from core.config import settings

    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    with TestClient(app) as client:
        if client.get("/health/db").status_code != 200:
            pytest.skip("Postgres unreachable")
        assert client.get("/market/strategies").status_code == 401
        token = client.post("/auth/signup", json={
            "email": f"bt-{uuid.uuid4().hex[:10]}@example.com",
            "password": "correct-horse-battery"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        catalogue = client.get("/market/strategies", headers=headers).json()
        assert len(catalogue) >= 12 and {"key", "params", "description"} <= set(catalogue[0])
        body = {"strategy": "rsi_reversion", "start": str(START), "end": str(END)}
        ran = client.post("/market/ZZCHART/backtest", json=body, headers=headers)
        assert ran.status_code == 200 and ran.json()["stats"]["final_equity"] > 0
        bad = client.post("/market/ZZCHART/backtest", headers=headers,
                          json={**body, "strategy": "astrology"})
        assert bad.status_code == 422 and "Known" in bad.json()["detail"]
        negative = client.post("/market/ZZCHART/backtest", headers=headers,
                               json={**body, "cost_bps": -1})
        assert negative.status_code == 422


def test_the_signal_endpoint_needs_a_login_and_returns_the_evidence(fake_market, monkeypatch):
    from api.main import app
    from core.config import settings

    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    with TestClient(app) as client:
        if client.get("/health/db").status_code != 200:
            pytest.skip("Postgres unreachable")
        assert client.get("/market/ZZCHART/signal").status_code == 401
        token = client.post("/auth/signup", json={
            "email": f"sig-{uuid.uuid4().hex[:10]}@example.com",
            "password": "correct-horse-battery"}).json()["access_token"]
        body = client.get("/market/ZZCHART/signal",
                          headers={"Authorization": f"Bearer {token}"}).json()
        assert body["ok"] and body["signal"] in ("BUY", "SELL", "HOLD")
        assert body["disclaimer"] == "Educational project, not investment advice."
        assert body["votes"] and body["provider"] == "fake" and body["currency"] == "USD"
