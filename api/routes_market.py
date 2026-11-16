"""Market data for the chart page.

    GET /market/{ticker}/chart?start=2025-11-01&end=2026-11-13&indicators=sma50,bollinger,rsi14
    GET /market/{ticker}/analytics?start=...&end=...&benchmark=^NSEI   (default: the ticker's market index)
    GET  /market/strategies
    GET  /market/watchlist           the dashboard: Nifty strip and the watchlist cards
    POST /market/{ticker}/backtest   {strategy, params, start, end, cost_bps, slippage_bps}
    GET  /market/{ticker}/signal     BUY / SELL / HOLD with its evidence (market/signals.py)
    GET  /market/{ticker}/signal/explain   the agent's explanation, local Ollama (market/explain.py)
    GET  /market/{ticker}/signal/report    evidence, verdict, curves, markers, cards (the stock page)
    GET  /market/{ticker}/options?structure=&days=30&lot_size=1   priced structures (market/options.py)

Authenticated like the rest of the app's API: the cache is shared and a
provider fetch is not free, so it is not an open endpoint. Dates default to
the last year up to today.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from auth.deps import get_current_user
from db.models import User
from market.providers import ProviderError
from market.service import (
    backtest,
    chart_data,
    parse_indicators,
    price_analytics,
    strategy_catalogue,
    trading_signal,
)

router = APIRouter(prefix="/market", tags=["market"])

MAX_RANGE_DAYS = 366 * 10


def _range(start: date | None, end: date | None) -> tuple[date, date]:
    end = end or date.today()
    start = start or end - timedelta(days=365)
    if start > end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "start is after end.")
    if (end - start).days > MAX_RANGE_DAYS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "At most ten years at once.")
    return start, end


@router.get("/{ticker}/chart")
async def chart(ticker: str, start: date | None = None, end: date | None = None,
                indicators: str = Query(default=""),
                user: User = Depends(get_current_user)) -> dict:
    start, end = _range(start, end)
    try:
        names = parse_indicators(indicators)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    try:
        data = await chart_data(ticker, start, end, names)
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"The price provider could not be reached: {exc}") from None
    if not data["bars"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"No price history for {data['ticker']} in that range.")
    return data


@router.get("/{ticker}/analytics")
async def analytics(ticker: str, start: date | None = None, end: date | None = None,
                    benchmark: str | None = None,
                    user: User = Depends(get_current_user)) -> dict:
    """No `benchmark`: the ticker's own market index. `benchmark=` (empty): none."""
    start, end = _range(start, end)
    try:
        return await price_analytics(ticker, start, end,
                                     "auto" if benchmark is None else (benchmark or None))
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"The price provider could not be reached: {exc}") from None
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None


@router.get("/watchlist")
async def watchlist_cards(user: User = Depends(get_current_user)) -> dict:
    """Index strip and one card per watchlist stock: price, change, a 60-day
    sparkline and the signal with its coverage."""
    from market.service import watchlist

    try:
        return await watchlist()
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"The price provider could not be reached: {exc}") from None


@router.get("/strategies")
async def strategies(user: User = Depends(get_current_user)) -> list[dict]:
    return strategy_catalogue()


class BacktestRequest(BaseModel):
    strategy: str
    params: dict[str, float] = Field(default_factory=dict)
    start: date | None = None
    end: date | None = None
    cost_bps: float = Field(default=5.0, ge=0, le=500)
    slippage_bps: float = Field(default=5.0, ge=0, le=500)
    initial_cash: float = Field(default=10_000.0, gt=0, le=1e12)


@router.post("/{ticker}/backtest")
async def run(ticker: str, body: BacktestRequest,
              user: User = Depends(get_current_user)) -> dict:
    start, end = _range(body.start, body.end)
    try:
        return await backtest(ticker, body.strategy, start, end, body.params,
                              body.cost_bps, body.slippage_bps, body.initial_cash)
    except KeyError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.args[0]) from None
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"The price provider could not be reached: {exc}") from None


@router.get("/{ticker}/signal")
async def signal(ticker: str, user: User = Depends(get_current_user)) -> dict:
    """Educational project, not investment advice: computed from past prices
    only, with every vote and weight in the response."""
    try:
        out = await trading_signal(ticker)
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"The price provider could not be reached: {exc}") from None
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    if not out.get("ok"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, out.get("error"))
    return out


@router.get("/{ticker}/signal/explain")
async def explain(ticker: str, user: User = Depends(get_current_user)) -> dict:
    """The agent's explanation of the signal, citing the strategy cards, with
    the backtest's verdict stated plainly. Runs on local Ollama: a hosted
    provider needs the owner's approval first."""
    from market.explain import explain_signal

    out = await explain_signal(ticker)
    if not out.get("ok"):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, out.get("error"))
    return out


@router.get("/{ticker}/options")
async def options(ticker: str, structure: str | None = None,
                  days: int = Query(default=30, ge=1, le=365),
                  lot_size: int = Query(default=1, ge=1, le=100_000),
                  user: User = Depends(get_current_user)) -> dict:
    """Option structures with Black-Scholes premium estimates, payoff,
    breakevens and maximum loss. Estimates, not quotes; not advice."""
    from market.service import option_ideas

    try:
        return await option_ideas(ticker, structure, days, lot_size)
    except KeyError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.args[0]) from None
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"The price provider could not be reached: {exc}") from None
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None


@router.get("/{ticker}/signal/report")
async def report(ticker: str, user: User = Depends(get_current_user)) -> dict:
    """The stock page's signal and backtest tabs in one response."""
    from market.service import signal_report

    try:
        return await signal_report(ticker)
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"The price provider could not be reached: {exc}") from None
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None

