"""Thin public Bybit V5 client. No keys and no order endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from signal_bot.tracker import Bar

INTERVAL = timedelta(minutes=5)
MAX_BARS = 1000


class MarketError(RuntimeError):
    pass


def closed_bars(rows: list[list[str]], now: datetime) -> list[Bar]:
    bars = [Bar(datetime.fromtimestamp(int(row[0]) / 1000, timezone.utc), *map(float, row[1:5])) for row in rows]
    return sorted((bar for bar in bars if bar.start + INTERVAL <= now), key=lambda bar: bar.start)


class BybitClient:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(base_url="https://api.bybit.com", timeout=15)

    async def _get(self, route: str, **params) -> dict:
        try:
            response = await self.client.get("/v5/market/" + route, params=params)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketError(type(exc).__name__) from exc
        if data.get("retCode") != 0:
            raise MarketError(f"retCode {data.get('retCode')}")
        return data["result"]

    async def symbols(self) -> set[str]:
        found, cursor = set(), None
        while True:
            params = {"category": "linear", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            result = await self._get("instruments-info", **params)
            found |= {item["symbol"] for item in result["list"] if item.get("status") == "Trading"
                      and item.get("contractType") == "LinearPerpetual" and item["symbol"].endswith("USDT")}
            cursor = result.get("nextPageCursor")
            if not cursor:
                return found

    async def price(self, symbol: str) -> float:
        result = await self._get("tickers", category="linear", symbol=symbol)
        return float(result["list"][0]["lastPrice"])

    async def bars(self, symbol: str, start: datetime, now: datetime) -> list[Bar]:
        begin = int(start.timestamp() * 1000)
        end = begin + (MAX_BARS - 1) * int(INTERVAL.total_seconds() * 1000)
        result = await self._get("kline", category="linear", symbol=symbol, interval="5",
                                 start=begin, end=end, limit=MAX_BARS)
        return closed_bars(result["list"], now)
