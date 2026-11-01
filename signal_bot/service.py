"""Business flow between posts, market and storage. Telegram-free."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from signal_bot.extract import Zone, extract_post
from signal_bot.hermes import BadAnswer, HermesError
from signal_bot.market import INTERVAL, MarketError
from signal_bot.tracker import Trade, advance

SYMBOLS_TTL = timedelta(hours=6)
ALBUM_PAUSE = timedelta(seconds=5)
RETRY_EVERY = timedelta(minutes=5)
MAX_RETRY = timedelta(hours=1)
MAX_DOUBLINGS = 8
MAX_HOPELESS_TRIES = 3
HOPELESS = {"IMAGE_REJECTED", "BAD_REQUEST", "UNEXPECTED_TOOL_USE"}
LINKED = {"update", "cancel", "result"}


@dataclass(frozen=True)
class Incoming:
    author: str
    origin_key: str | None
    url: str | None
    text: str
    posted_at: datetime
    part_key: str
    message_id: int
    album: str | None = None
    file_id: str | None = None
    reply_to: int | None = None


@dataclass(frozen=True)
class Outcome:
    kind: str
    post_id: int | None = None


@dataclass(frozen=True)
class Analysis:
    kind: str
    unclear: tuple[Zone, ...] = ()


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


def accept(storage, post: Incoming, image: str | None, now: datetime) -> Outcome:
    """Save a post before reading it: whatever fails later, the post is already safe."""
    ready_at = now + ALBUM_PAUSE if post.album else now
    stored = storage.add_part(post.author, post.origin_key, post.url, post.text, post.posted_at, post.album,
                              post.part_key, post.message_id, image, post.file_id, post.reply_to, ready_at, now)
    if stored is None:
        return Outcome("duplicate")
    post_id, created = stored
    return Outcome("queued" if created else "joined", post_id)


def find_link(open_zones: list[dict], zone: Zone) -> tuple[dict | None, int]:
    """The model proposes, the code decides: only an open zone of this author, coin and side counts."""
    same = [item for item in open_zones if item["symbol"] == zone.symbol and zone.side in (None, item["side"])]
    if zone.replaces is not None:
        return next((item for item in same if item["id"] == zone.replaces), None), len(same)
    return (same[0] if len(same) == 1 else None), len(same)


async def _fields(zone: Zone, market) -> dict:
    price = None
    if "UNKNOWN_SYMBOL" not in zone.flags:
        try:
            price = await market.price(zone.symbol)
        except MarketError:
            pass
    return {**asdict(zone), "targets": list(zone.targets), "flags": list(zone.flags), "price": price}


async def analyze_post(storage, broker, market, post: dict, symbols: set[str], now: datetime) -> Analysis:
    open_zones = storage.open_zones(post["author"])
    found = await extract_post(broker, post["edited_text"] or post["text"], storage.post_images(post["id"]),
                               datetime.fromisoformat(post["posted_at"]), symbols, market.price_at,
                               open_zones=open_zones)
    items, unclear = [], []
    for zone in found.zones:
        target, candidates = find_link(open_zones, zone) if zone.action in LINKED else (None, 0)
        if target:
            items.append((zone.action, await _fields(zone, market), target["id"]))
        elif zone.action in {"new", "update"}:
            items.append(("new", await _fields(zone, market), None))
        elif candidates:
            unclear.append(zone)
    kind = "zones" if items else "opinion"
    storage.save_parse(post["id"], kind, items, now)
    return Analysis(kind, tuple(unclear))


def next_try(post: dict, error: Exception, now: datetime) -> datetime | None:
    """An outage waits as long as it takes; an answer that cannot be read stops after a few tries."""
    hopeless = (not isinstance(error, HermesError) or isinstance(error, BadAnswer) or error.code in HOPELESS)
    if hopeless and post["attempts"] + 1 >= MAX_HOPELESS_TRIES:
        return None
    return now + min(RETRY_EVERY * 2 ** min(post["attempts"], MAX_DOUBLINGS), MAX_RETRY)


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
