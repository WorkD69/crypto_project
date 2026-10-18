"""Closed-candle operational proxy for the public liquidity lesson.

The author's qualitative concept is prior/equal extrema where stops may rest.
Pivot width and tolerance below are our own reproducible research choices.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from scripts.mvp_scenario import Candle


RULE_VERSION = "liquidity-watch-0.2"
METHOD_URL = "https://telegra.ph/Likvidnost-poisk-tochek-vhoda-07-27-2"


def validate_snapshot(candles: list[Candle]) -> None:
    if any(c.start.tzinfo is None or c.high < max(c.open, c.close, c.low)
           or c.low > min(c.open, c.close, c.high) for c in candles):
        raise ValueError("Invalid candle timestamp or OHLC")
    if any(a.start >= b.start for a, b in zip(candles, candles[1:])):
        raise ValueError("Candles must be unique and chronological")


def _pivots(candles: list[Candle], side: str, width: int = 2) -> list[int]:
    prices = [c.low if side == "low" else c.high for c in candles]
    result = []
    for i in range(width, len(prices) - width):
        nearby = prices[i-width:i] + prices[i+1:i+width+1]
        if (prices[i] < min(nearby) if side == "low" else prices[i] > max(nearby)):
            result.append(i)
    return result


def find_observation(candles: list[Candle], *, as_of: datetime,
                     interval: timedelta, tolerance: float = .002) -> dict:
    """Find the nearest unbroken equal-extrema pool, then a single swing.

    This is a watch area, never an order or a claim that stops really exist.
    """
    if not 0 < tolerance <= .02 or interval <= timedelta(0) or as_of.tzinfo is None:
        raise ValueError("Invalid research parameters")
    validate_snapshot(candles)
    closed = [c for c in candles if c.start + interval <= as_of]
    base = {"status": "NO_AREA", "direction": None, "zone_kind": None,
            "zone": None, "entry": None, "sl": None, "targets": [], "rr": None,
            "rule_version": RULE_VERSION, "source": METHOD_URL,
            "as_of": as_of.isoformat(), "closed_candles": len(closed)}
    if len(closed) < 9:
        return {**base, "reason": "fewer than nine closed candles"}
    price = closed[-1].close
    choices = []
    for side in ("low", "high"):
        pivots = _pivots(closed, side)
        for a_pos, a in enumerate(pivots):
            for b in pivots[a_pos + 1:]:
                if b - a < 3 or b - a > 50:
                    continue
                first = closed[a].low if side == "low" else closed[a].high
                second = closed[b].low if side == "low" else closed[b].high
                if abs(first - second) / min(first, second) > tolerance:
                    continue
                low, high = sorted((first, second))
                if side == "low":
                    if high >= price or any(c.low < low for c in closed[b+1:]):
                        continue
                    distance = (price - high) / price
                    direction = "LONG"
                else:
                    if low <= price or any(c.high > high for c in closed[b+1:]):
                        continue
                    distance = (low - price) / price
                    direction = "SHORT"
                choices.append((distance, -b, direction, low, high, a, b))
    if choices:
        _, _, direction, low, high, a, b = min(choices)
        return {**base, "status": "WATCHING", "direction": direction,
                "zone_kind": "equal_lows" if direction == "LONG" else "equal_highs",
                "zone": [low, high], "pivot_starts": [closed[a].start.isoformat(),
                                                       closed[b].start.isoformat()],
                "last_closed_start": closed[-1].start.isoformat(),
                "last_closed_price": price,
                "reason": "two confirmed equal extrema remain unswept"}
    # The lesson also points to stops beyond a previous minimum/maximum.
    # A single pivot is useful as a watch level, with no invented confluence.
    singles = []
    for side in ("low", "high"):
        for i in _pivots(closed, side):
            level = closed[i].low if side == "low" else closed[i].high
            if side == "low" and level < price and all(c.low >= level for c in closed[i+1:]):
                singles.append(((price-level)/price, -i, "LONG", level, i))
            if side == "high" and level > price and all(c.high <= level for c in closed[i+1:]):
                singles.append(((level-price)/price, -i, "SHORT", level, i))
    if singles:
        _, _, direction, level, i = min(singles)
        return {**base, "status": "WATCHING", "direction": direction,
                "zone_kind": "prior_low" if direction == "LONG" else "prior_high",
                "zone": [level, level], "pivot_starts": [closed[i].start.isoformat()],
                "last_closed_start": closed[-1].start.isoformat(),
                "last_closed_price": price,
                "reason": "confirmed prior extremum remains unswept"}
    return {**base, "reason": "no unbroken prior extremum around price"}


def daily_context(candles: list[Candle], *, as_of: datetime) -> dict:
    validate_snapshot(candles)
    closed = [c for c in candles if c.start + timedelta(days=1) <= as_of]
    if len(closed) < 2:
        raise ValueError("At least two closed daily candles required")
    window = closed[-20:]
    return {"timeframe": "1D", "start": window[0].start.isoformat(),
            "end": window[-1].start.isoformat(),
            "range_low": min(c.low for c in window),
            "range_high": max(c.high for c in window),
            "last_close": window[-1].close,
            "change_from_previous_close_pct": round(
                (window[-1].close / window[-2].close - 1) * 100, 2)}
