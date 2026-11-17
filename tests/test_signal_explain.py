"""The backtest verdict, the agent's signal tool, and the explanation checks.

The verdict and the explanation checks need nothing: the agent is replaced by
a stub that returns a chosen answer, so what is tested is what Python does
with it. The tool test needs Postgres (the price cache and the strategy
cards) and skips without it. The live test needs the API proxy and a model
behind it, like test_injection's, and skips without them.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from market.explain import explain_signal
from market.signal_eval import EDGE_PERCENTILE, backtest_verdict


def _ev(sig_cagr, bh_cagr, sig_sharpe, bh_sharpe, pct, years=4.9):
    return {"ok": True, "years": years, "trading_days": int(years * 252),
            "start": "2021-11-16", "end": "2026-11-16",
            "random": {"signal_sharpe_percentile": pct, "runs": 200},
            "risk": {"signal": {"cagr": sig_cagr, "sharpe": sig_sharpe, "max_drawdown": -0.3,
                                "time_in_market": 0.5},
                     "buy_and_hold": {"cagr": bh_cagr, "sharpe": bh_sharpe,
                                      "max_drawdown": -0.24, "time_in_market": 1.0}}}


def test_no_edge_is_the_verdict_unless_return_sharpe_and_timing_all_win():
    sbin = backtest_verdict("SBIN.NS", _ev(-0.0299, 0.1966, -0.0747, 0.8578, 0.0))
    assert sbin["verdict"] == "no_edge"
    assert sbin["statement"].startswith("The 4.9-year backtest shows no edge for SBIN.NS:")
    assert "-3.0% a year against 19.7%" in sbin["statement"]
    # Better return and Sharpe, but timing no better than luck: still no edge.
    assert backtest_verdict("X", _ev(0.2, 0.1, 1.0, 0.5, 0.9))["verdict"] == "no_edge"
    assert backtest_verdict("X", _ev(0.2, 0.1, 0.4, 0.5, 0.99))["verdict"] == "no_edge"
    win = backtest_verdict("X", _ev(0.2, 0.1, 1.0, 0.5, EDGE_PERCENTILE))
    assert win["verdict"] == "edge" and "do not guarantee" in win["statement"]


def test_a_short_window_says_it_is_too_short_and_an_untested_one_says_why():
    short = backtest_verdict("PREMIERENE.NS", _ev(-0.027, -0.064, -0.01, -0.02, 0.52, 1.61))
    assert "Only 1.61 years could be tested" in short["statement"]
    assert backtest_verdict("NEW.NS", {"ok": False, "error": "only 20 bars"})["verdict"] == "untested"


def _stub(answer, signal_result, tools=("get_trading_signal",)):
    async def run(question, **kwargs):
        run.kwargs = kwargs
        steps = [SimpleNamespace(tool=t, ok=True, result=signal_result) for t in tools]
        return SimpleNamespace(answer=answer, steps=steps)
    return run


SIGNAL = {"ok": True, "ticker": "SBIN.NS", "as_of": "2026-11-16", "signal": "BUY",
          "score": 0.5032, "confidence": 0.5032, "coverage": "12 of 16 rules",
          "backtest": backtest_verdict("SBIN.NS", _ev(-0.0299, 0.1966, -0.0747, 0.8578, 0.0)),
          "cards": [{"card": "adx_trend", "section": "Summary", "text": "...",
                     "source": "docs/strategies/adx_trend.md"}]}


def test_the_verdict_is_put_first_verbatim_and_a_missing_no_edge_is_recorded():
    run = _stub("The signal engine's output is BUY on 12 of 16 rules "
                "(docs/strategies/adx_trend.md).", SIGNAL)
    out = asyncio.run(explain_signal("sbin.ns", run=run))
    assert out["ok"] and out["answer"].startswith(SIGNAL["backtest"]["statement"])
    assert out["added"] == ["backtest_statement", "disclaimer"]
    assert out["model_said_no_edge"] is False
    assert out["answer"].endswith("Educational project, not investment advice.")
    assert out["cards_cited_by_model"] == ["adx_trend"]
    assert run.kwargs["provider"] == "ollama" and run.kwargs["router_provider"] == "ollama"


def test_an_answer_that_already_says_no_edge_still_gets_the_verbatim_verdict_first():
    text = ("The signal engine's output is BUY (docs/strategies/adx_trend.md). The backtest "
            "shows no edge for SBIN.NS. Educational project, not investment advice.")
    out = asyncio.run(explain_signal("SBIN.NS", run=_stub(text, SIGNAL)))
    assert out["answer"] == SIGNAL["backtest"]["statement"] + "\n\n" + text
    assert out["added"] == ["backtest_statement"] and out["model_said_no_edge"] is True


def test_an_answer_naming_no_card_gets_the_supplied_cards_listed():
    out = asyncio.run(explain_signal("SBIN.NS", run=_stub(
        "The signal engine's output is BUY. The backtest shows no edge.", SIGNAL)))
    assert "card_sources" in out["added"]
    assert "Strategy cards used: docs/strategies/adx_trend.md" in out["answer"]
    assert out["cards_supplied"] == ["docs/strategies/adx_trend.md"]


def test_a_run_that_did_not_finish_is_a_failure_with_nothing_added():
    async def run(question, **kwargs):
        return SimpleNamespace(answer="I couldn't reach the model to answer this (ReadTimeout).",
                               steps=[SimpleNamespace(tool="get_trading_signal", ok=True,
                                                      result=SIGNAL)],
                               completed=False, stop_reason="inference_error")
    out = asyncio.run(explain_signal("SBIN.NS", run=run))
    assert not out["ok"] and "inference_error" in out["error"]
    assert "no edge" not in out["answer"]


def test_without_the_signal_tool_there_is_no_explanation():
    out = asyncio.run(explain_signal("SBIN.NS", run=_stub("SBI looks fine.", SIGNAL,
                                                          tools=("search_news",))))
    assert not out["ok"] and "did not fetch the signal" in out["error"]


def test_the_tool_is_registered_with_its_instructions():
    from agent.tools import build_registry
    registry = build_registry()
    assert "get_trading_signal" in registry.names
    description = " ".join(registry.get("get_trading_signal").schema()["function"]["description"].split())
    assert "no edge" in description and "never as advice" in description


def test_the_tool_returns_the_evidence_the_verdict_and_the_cards():
    """Live Postgres: the fake provider fills the cache, the cards come from
    strategy_cards (skips if either is missing)."""
    from agent import tools as agent_tools
    from db.base import engine
    from market import service
    from market.store import MarketData
    from tests.test_market_api import Fake

    from sqlalchemy import text

    async def go():
        try:
            return await agent_tools.get_trading_signal("ZZCHART")
        finally:
            async with engine.begin() as conn:
                for table in ("price_bars", "price_fetches"):
                    await conn.execute(text(f"DELETE FROM {table} WHERE ticker = 'ZZCHART'"))
                    # The fake feed also served the default benchmark (SPY for a
                    # US ticker): remove only its rows, never real ones.
                    await conn.execute(text(f"DELETE FROM {table} WHERE ticker IN ('SPY', '^NSEI') "
                                            f"AND provider = 'fake'"))
            await engine.dispose()

    original = service.MarketData
    service.MarketData = lambda: MarketData(Fake())
    try:
        out = asyncio.run(go())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not available ({type(exc).__name__})")
    finally:
        service.MarketData = original
    assert out["ok"], out
    assert out["signal"] in ("BUY", "SELL", "HOLD") and " of " in out["coverage"]
    assert out["backtest"]["verdict"] in ("edge", "no_edge", "untested")
    assert out["backtest"]["statement"]
    assert out["disclaimer"] == "Educational project, not investment advice."
    if out["cards"]:
        assert all(c["source"].startswith("docs/strategies/") for c in out["cards"])
    else:
        assert out["cards_note"] == "strategy cards are not indexed"


def test_the_random_share_is_stated_exactly_not_rounded_two_ways():
    assert "beat 44.5% of 200" in backtest_verdict("X", _ev(0.1, 0.2, 0.5, 0.9, 0.445))["statement"]
    assert "beat 0% of 200" in backtest_verdict("X", _ev(0.1, 0.2, 0.5, 0.9, 0.0))["statement"]
    assert "beat 89% of 200" in backtest_verdict("X", _ev(0.1, 0.2, 0.5, 0.9, 0.89))["statement"]
