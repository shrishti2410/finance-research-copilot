"""Market data for the chart page.

    GET /market/{ticker}/chart?start=2025-11-01&end=2026-11-13&indicators=sma50,bollinger,rsi14
    GET /market/{ticker}/analytics?start=...&end=...&benchmark=SPY

Authenticated like the rest of the app's API: the cache is shared and a
provider fetch is not free, so it is not an open endpoint. Dates default to
the last year up to today.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status

from auth.deps import get_current_user
from db.models import User
from market.providers import ProviderError
from market.service import DEFAULT_BENCHMARK, chart_data, parse_indicators, price_analytics

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
                    benchmark: str = DEFAULT_BENCHMARK,
                    user: User = Depends(get_current_user)) -> dict:
    start, end = _range(start, end)
    try:
        return await price_analytics(ticker, start, end, benchmark or None)
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"The price provider could not be reached: {exc}") from None
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
