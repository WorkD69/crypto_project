"""Pure paper tracking of one idea on closed candles; no exchange orders."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

TAKER_FEE = 0.00055  # Bybit linear taker fee per side.
MAX_AGE = timedelta(days=14)
FINISHED = {"SL", "TP", "TIMEOUT", "MISSED", "EXPIRED", "NOT_TRACKED"}


@dataclass(frozen=True)
class Bar:
    start: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Trade:
    side: str
    entry_low: float
    entry_high: float
    stop: float | None
    target: float | None
    created_at: datetime
    status: str = "WAITING"
    fill_price: float | None = None
    exit_price: float | None = None
    result_r: float | None = None
    ambiguous: bool = False
    last_bar: datetime | None = None

    @property
    def entry(self) -> float:
        return (self.entry_low + self.entry_high) / 2


def _valid(trade: Trade) -> bool:
    if trade.stop is None:
        return False
    if trade.side == "LONG":
        return trade.stop < trade.entry_low and (trade.target is None or trade.target > trade.entry_high)
    return trade.stop > trade.entry_high and (trade.target is None or trade.target < trade.entry_low)


def reward_risk(trade: Trade) -> float | None:
    if trade.stop is None or trade.target is None or trade.stop == trade.entry:
        return None
    return abs(trade.target - trade.entry) / abs(trade.entry - trade.stop)


def _close(trade: Trade, status: str, exit_price: float | None, ambiguous: bool = False) -> Trade:
    fill, stop = trade.fill_price, trade.stop
    assert fill is not None and stop is not None and exit_price is not None
    risk = abs(fill - stop)
    sign = 1 if trade.side == "LONG" else -1
    gross = sign * (exit_price - fill) / risk
    fees = (fill + exit_price) * TAKER_FEE / risk
    return replace(trade, status=status, exit_price=exit_price, ambiguous=ambiguous,
                   result_r=round(gross - fees, 4))


def _hits(trade: Trade, bar: Bar) -> tuple[bool, bool]:
    stop, target = trade.stop, trade.target
    assert stop is not None
    if trade.side == "LONG":
        return bar.low <= stop, target is not None and bar.high >= target
    return bar.high >= stop, target is not None and bar.low <= target


def _step(trade: Trade, bar: Bar) -> Trade:
    aged = bar.start >= trade.created_at + MAX_AGE
    if trade.status == "WAITING":
        if aged:
            return replace(trade, status="EXPIRED")
        if bar.low <= trade.entry_high and bar.high >= trade.entry_low:
            trade = replace(trade, status="ENTERED", fill_price=trade.entry)
            # A target inside the fill candle is unprovable; its stop is not forgiven.
            if _hits(trade, bar)[0]:
                return _close(trade, "SL", trade.stop)
            return trade
        return replace(trade, status="MISSED") if _hits(trade, bar)[1] else trade
    hit_stop, hit_target = _hits(trade, bar)
    if hit_stop:
        return _close(trade, "SL", trade.stop, ambiguous=hit_target)
    if hit_target:
        return _close(trade, "TP", trade.target)
    return _close(trade, "TIMEOUT", bar.close) if aged else trade


def advance(trade: Trade, bars: list[Bar]) -> Trade:
    """Apply closed candles in order; each candle counts once."""
    if trade.status == "WAITING" and not _valid(trade):
        return replace(trade, status="NOT_TRACKED")
    for bar in sorted(bars, key=lambda b: b.start):
        if trade.status in FINISHED:
            break
        if bar.start < trade.created_at or (trade.last_bar and bar.start <= trade.last_bar):
            continue
        trade = replace(_step(trade, bar), last_bar=bar.start)
    return trade
