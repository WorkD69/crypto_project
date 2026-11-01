"""Business flow between posts, market and storage. Telegram-free."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from signal_bot.market import INTERVAL, MarketError
from signal_bot.parser import apply_price, parse_post
from signal_bot.tracker import Trade, advance

SYMBOLS_TTL = timedelta(hours=6)


@dataclass(frozen=True)
class Incoming:
    author: str
    origin_key: str | None
    url: str | None
    text: str


@dataclass(frozen=True)
class Outcome:
    kind: str
    idea_id: int | None = None


@dataclass(frozen=True)
class Change:
    idea: dict
    trade: Trade


class SymbolCache:
    def __init__(self, market):
        self.market = market
        self.value: set[str] = set()
        self.loaded_at: datetime | None = None

    async def get(self, now: datetime) -> set[str]:
        if self.loaded_at is None or now - self.loaded_at >= SYMBOLS_TTL:
            try:
                self.value = await self.market.symbols()
                self.loaded_at = now
            except MarketError:
                pass  # A stale list is better than treating every idea as an opinion.
        return self.value


async def process_post(storage, market, post: Incoming, symbols: set[str], now: datetime) -> Outcome:
    text = post.text.strip()
    if not text:
        return Outcome("empty")
    parsed = parse_post(text, symbols)
    price, levels = None, None
    if parsed.is_idea:
        try:
            price = await market.price(parsed.symbol)
        except MarketError:
            pass
        levels = apply_price(parsed, price)
    # Real author prose mentions coins and levels constantly; only a stop makes it a trade.
    kind = "idea" if levels and levels.stop is not None else "opinion"
    post_id = storage.add_post(post.author, post.origin_key, post.url, text, kind, now)
    if post_id is None:
        return Outcome("duplicate")
    if kind == "opinion":
        return Outcome("opinion")
    entry_low, entry_high, assumed = levels.entry_low, levels.entry_high, False
    if entry_low is None and price is not None:
        entry_low, entry_high, assumed = price, price, True
    warnings, status = list(levels.warnings), "NOT_TRACKED"
    if levels.side and entry_low is not None and levels.stop is not None:
        trade = Trade(levels.side, entry_low, entry_high, levels.stop,
                      levels.targets[0] if levels.targets else None, now)
        status = advance(trade, []).status
        if status == "NOT_TRACKED":
            warnings.append("BAD_LEVEL_ORDER")
    idea_id = storage.add_idea(post_id, levels.symbol, levels.side, entry_low, entry_high, levels.stop,
                               levels.targets, assumed, price, warnings, now, status=status)
    return Outcome("idea", idea_id)


def _next_candle(moment: datetime) -> datetime:
    step = INTERVAL.total_seconds()
    return datetime.fromtimestamp(-(-moment.timestamp() // step) * step, timezone.utc)


async def track_open(storage, market, now: datetime) -> list[Change]:
    changes = []
    for idea in storage.open_ideas():
        trade = storage.trade(idea)
        start = trade.last_bar + INTERVAL if trade.last_bar else _next_candle(trade.created_at)
        try:
            bars = await market.bars(idea["symbol"], start, now)
        except MarketError:
            continue
        updated = advance(trade, bars)
        if updated != trade:
            storage.save_trade(idea["id"], updated)
        if updated.status != trade.status:
            changes.append(Change(idea, updated))
    return changes
