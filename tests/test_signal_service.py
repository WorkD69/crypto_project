import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from signal_bot.market import MarketError
from signal_bot.service import Incoming, SymbolCache, process_post, track_open
from signal_bot.storage import Storage
from signal_bot.tracker import Bar

T0 = datetime(2026, 11, 1, 12, 2, tzinfo=timezone.utc)
SYMBOLS = {"BTCUSDT"}


class FakeMarket:
    def __init__(self, price=80000.0, bars=None, broken=False):
        self.last_price, self.last_bars, self.broken = price, bars or [], broken
        self.starts = []

    async def price(self, symbol):
        if self.broken:
            raise MarketError("down")
        return self.last_price

    async def bars(self, symbol, start, now):
        if self.broken:
            raise MarketError("down")
        self.starts.append(start)
        return self.last_bars


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = Storage(Path(self.directory.name) / "bot.sqlite3")

    def process(self, text, market=None, key=None):
        post = Incoming("Илья", key, None, text)
        return asyncio.run(process_post(self.storage, market or FakeMarket(), post, SYMBOLS, T0))


class ProcessPostTest(ServiceCase):
    def test_full_idea_is_tracked(self):
        outcome = self.process("BTC лонг вход 79900 стоп 78000 тейк 84000")
        idea = self.storage.get_idea(outcome.idea_id)
        self.assertEqual(outcome.kind, "idea")
        self.assertEqual(idea["status"], "WAITING")
        self.assertEqual(idea["price"], 80000)

    def test_opinion_is_stored_without_idea(self):
        outcome = self.process("Рынок выглядит слабым")
        self.assertEqual(outcome.kind, "opinion")
        self.assertEqual(len(self.storage.posts_since(T0, "opinion")), 1)

    def test_duplicate_post(self):
        self.process("BTC лонг стоп 78000", key="c:1")
        self.assertEqual(self.process("BTC лонг стоп 78000", key="c:1").kind, "duplicate")

    def test_blank_text_is_ignored(self):
        self.assertEqual(self.process("   ").kind, "empty")

    def test_missing_entry_uses_market_price(self):
        idea = self.storage.get_idea(self.process("BTC лонг стоп 78000").idea_id)
        self.assertEqual(idea["entry_low"], 80000)
        self.assertTrue(idea["entry_assumed"])
        self.assertEqual(idea["status"], "WAITING")

    def test_levels_without_stop_are_an_opinion(self):
        self.assertEqual(self.process("BTC лонг вход 80000 тейк 84000").kind, "opinion")

    def test_interest_zone_without_stop_is_an_opinion(self):
        self.assertEqual(self.process("Биткоин: вход в зоне 72000-74000 посмотрим на реакцию").kind, "opinion")

    def test_all_levels_far_from_price_are_an_opinion(self):
        self.assertEqual(self.process("BTC лонг, цель 2 года, стоп 15").kind, "opinion")
        self.assertEqual(len(self.storage.posts_since(T0, "idea")), 0)

    def test_market_failure_still_saves_idea(self):
        outcome = self.process("BTC лонг вход 80000 стоп 78000", FakeMarket(broken=True))
        idea = self.storage.get_idea(outcome.idea_id)
        self.assertIsNone(idea["price"])
        self.assertIn("PRICE_UNAVAILABLE", idea["warnings"])
        self.assertEqual(idea["status"], "WAITING")

    def test_market_failure_without_entry_is_not_tracked(self):
        outcome = self.process("BTC лонг стоп 78000", FakeMarket(broken=True))
        self.assertEqual(self.storage.get_idea(outcome.idea_id)["status"], "NOT_TRACKED")


    def test_wrong_level_order_is_not_tracked(self):
        idea = self.storage.get_idea(self.process("BTC лонг вход 80000 стоп 82000").idea_id)
        self.assertEqual(idea["status"], "NOT_TRACKED")
        self.assertIn("BAD_LEVEL_ORDER", idea["warnings"])


class SymbolCacheTest(unittest.TestCase):
    class Market:
        def __init__(self):
            self.calls, self.broken = 0, False

        async def symbols(self):
            self.calls += 1
            if self.broken:
                raise MarketError("down")
            return {"BTCUSDT"}

    def test_fresh_list_is_reused(self):
        market = self.Market()
        cache = SymbolCache(market)
        asyncio.run(cache.get(T0))
        asyncio.run(cache.get(T0 + timedelta(hours=1)))
        self.assertEqual(market.calls, 1)

    def test_old_list_is_refreshed(self):
        market = self.Market()
        cache = SymbolCache(market)
        asyncio.run(cache.get(T0))
        asyncio.run(cache.get(T0 + timedelta(hours=7)))
        self.assertEqual(market.calls, 2)

    def test_failure_keeps_old_list(self):
        market = self.Market()
        cache = SymbolCache(market)
        asyncio.run(cache.get(T0))
        market.broken = True
        self.assertEqual(asyncio.run(cache.get(T0 + timedelta(hours=7))), {"BTCUSDT"})

    def test_failure_without_list_gives_empty_set(self):
        market = self.Market()
        market.broken = True
        self.assertEqual(asyncio.run(SymbolCache(market).get(T0)), set())


class TrackOpenTest(ServiceCase):
    def idea(self):
        return self.process("BTC лонг вход 80000 стоп 78000 тейк 84000").idea_id

    def bar(self, minutes, high, low):
        return Bar(T0 + timedelta(minutes=minutes), low, high, low, low)

    def track(self, market):
        return asyncio.run(track_open(self.storage, market, T0 + timedelta(hours=1)))

    def test_status_change_is_saved_and_reported(self):
        idea_id = self.idea()
        changes = self.track(FakeMarket(bars=[self.bar(5, 80500, 79900)]))
        self.assertEqual(changes[0].trade.status, "ENTERED")
        self.assertEqual(self.storage.get_idea(idea_id)["status"], "ENTERED")

    def test_no_change_reports_nothing_but_moves_last_bar(self):
        idea_id = self.idea()
        self.assertEqual(self.track(FakeMarket(bars=[self.bar(5, 81000, 80500)])), [])
        self.assertIsNotNone(self.storage.get_idea(idea_id)["last_bar"])

    def test_first_request_starts_from_next_whole_candle(self):
        self.idea()
        market = FakeMarket(bars=[])
        self.track(market)
        self.assertEqual(market.starts, [datetime(2026, 11, 1, 12, 5, tzinfo=timezone.utc)])

    def test_next_request_starts_after_last_bar(self):
        self.idea()
        self.track(FakeMarket(bars=[self.bar(3, 81000, 80500)]))
        market = FakeMarket(bars=[])
        self.track(market)
        self.assertEqual(market.starts, [T0 + timedelta(minutes=8)])

    def test_market_failure_keeps_idea_open(self):
        idea_id = self.idea()
        self.assertEqual(self.track(FakeMarket(broken=True)), [])
        self.assertEqual(self.storage.get_idea(idea_id)["status"], "WAITING")


if __name__ == "__main__":
    unittest.main()
