"""Explicit test rules on closed candles. Not an author's method or trade signal."""
from dataclasses import dataclass
from datetime import datetime, timedelta
import math


@dataclass(frozen=True)
class Candle:
    start: datetime
    open: float
    high: float
    low: float
    close: float


def analyze(candles: list[Candle], *, as_of: datetime, interval: timedelta,
            low_anchor: int, high_anchor: int, zone_candle_index: int,
            min_rr: float = 1.5, tick_size: float | None = None) -> dict:
    """Return an observed candidate or an explicit refusal; no future bars used."""
    result = {"status": "REJECTED", "reason": None, "direction": None,
              "entry": None, "sl": None, "tp": None, "rr": None}
    closed = [c for c in candles if c.start + interval <= as_of]
    if (len(closed) < 5 or low_anchor < 0 or high_anchor < 0 or
            low_anchor >= len(closed) or high_anchor >= len(closed) or
            zone_candle_index < 0 or zone_candle_index >= len(closed) or
            zone_candle_index <= max(low_anchor, high_anchor) or
            low_anchor == high_anchor or
            (tick_size is not None and tick_size <= 0)):
        return {**result, "reason": "insufficient closed bars or invalid anchors"}
    if any(c.high < max(c.open, c.close, c.low) or
           c.low > min(c.open, c.close, c.high) for c in closed):
        return {**result, "reason": "invalid OHLC"}
    low = closed[low_anchor].low
    high = closed[high_anchor].high
    if high <= low:
        return {**result, "reason": "invalid anchor range"}
    direction = "LONG" if low_anchor < high_anchor else "SHORT"
    span = high - low
    if direction == "LONG":
        fib = (high - .618 * span, high - .5 * span)
    else:
        fib = (low + .5 * span, low + .618 * span)
    # This area is selected by an explicit closed candle ID, independently of Fibonacci.
    touched = closed[zone_candle_index]
    test_zone = (touched.low, touched.high)
    overlap = (max(fib[0], test_zone[0]), min(fib[1], test_zone[1]))
    if overlap[0] > overlap[1]:
        return {**result, "direction": direction, "fib_zone": fib,
                "test_zone": test_zone, "reason": "no zone overlap"}
    entry = sum(overlap) / 2
    ranges = [c.high - c.low for c in closed[-8:]]
    buffer = sum(ranges) / len(ranges) * .25
    sl = test_zone[0] - buffer if direction == "LONG" else test_zone[1] + buffer
    tp = high if direction == "LONG" else low
    if tick_size is not None:
        entry = round(round(entry / tick_size) * tick_size, 8)
        sl_steps = math.floor(sl / tick_size) if direction == "LONG" else math.ceil(sl / tick_size)
        tp_steps = math.floor(tp / tick_size) if direction == "LONG" else math.ceil(tp / tick_size)
        sl = round(sl_steps * tick_size, 8)
        tp = round(tp_steps * tick_size, 8)
    risk = abs(entry - sl)
    reward = tp - entry if direction == "LONG" else entry - tp
    rr = reward / risk if risk > 0 else 0
    common = {**result, "direction": direction, "fib_zone": fib,
              "test_zone": test_zone, "overlap": overlap,
              "zone_candle_start": touched.start.isoformat(),
              "anchor_low_start": closed[low_anchor].start.isoformat(),
              "anchor_high_start": closed[high_anchor].start.isoformat(),
              "last_closed_start": closed[-1].start.isoformat(),
              "last_closed_price": closed[-1].close}
    if not (sl < entry < tp if direction == "LONG" else tp < entry < sl):
        return {**common, "reason": "invalid level order"}
    if abs(closed[-1].close - entry) > sum(ranges) / len(ranges):
        return {**common, "reason": "price no longer near entry"}
    if rr < min_rr:
        return {**common, "reason": "risk/reward below threshold", "rr_observed": rr}
    return {**common, "status": "CANDIDATE", "reason": "test rules satisfied",
            "entry": entry, "sl": sl, "tp": tp, "rr": rr}
