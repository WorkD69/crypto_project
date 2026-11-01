import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from signal_bot.extract import parse_answer
from signal_bot.hermes import BadAnswer, HermesError, Reply
from signal_bot.market import MarketError
from signal_bot.service import ALBUM_PAUSE, MAX_RETRY, RETRY_EVERY, Incoming, SymbolCache, accept, analyze_post, \
    find_link, next_try, track_open
from signal_bot.storage import Storage
from signal_bot.tracker import Bar

T0 = datetime(2026, 11, 1, 12, 2, tzinfo=timezone.utc)
SYMBOLS = {"BTCUSDT", "ETHUSDT"}


def raw_zone(**changes):
    zone = {"symbol": "BTCUSDT", "side": "SHORT", "zone_low": 82000, "zone_high": 84000, "stop": None,
            "targets": [78000], "timeframe": "4H", "action": "new", "confidence": 0.8, "evidence": "красная рамка",
            "chart_price": 80000}
    return {**zone, **changes}


def answer(*zones):
    return {"relevant": bool(zones), "zones": list(zones)}


class FakeMarket:
    def __init__(self, price=80000.0, bars=None, broken=False):
        self.last_price, self.last_bars, self.broken = price, bars or [], broken
        self.starts = []

    async def price(self, symbol):
        if self.broken:
            raise MarketError("down")
        return self.last_price

    async def price_at(self, symbol, moment):
        if self.broken:
            raise MarketError("down")
        return self.last_price

    async def bars(self, symbol, start, now):
        if self.broken:
            raise MarketError("down")
        self.starts.append(start)
        return self.last_bars


class FakeBroker:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    async def ask(self, prompt, image_sha256=None, validate=None, live=True):
        self.calls.append((prompt, image_sha256))
        reply = self.answers.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Reply(reply, {}, 1, validate(reply))


def incoming(text="шорт 82000-84000", key="c:5", **overrides):
    fields = dict(author="Автор", origin_key=key, url=None, text=text, posted_at=T0, part_key=key, message_id=5)
    return Incoming(**{**fields, **overrides})


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = Storage(Path(self.directory.name) / "bot.sqlite3")

    def analyze(self, broker, key="c:5", text="шорт 82000-84000", market=None, image=None, author="Автор"):
        post = accept(self.storage, incoming(text, key, author=author), image, T0)
        return asyncio.run(analyze_post(self.storage, broker, market or FakeMarket(),
                                        self.storage.get_post(post.post_id), SYMBOLS, T0)), post.post_id


class AcceptTest(ServiceCase):
    def test_post_enters_the_queue_at_once(self):
        outcome = accept(self.storage, incoming(), None, T0)
        self.assertEqual(outcome.kind, "queued")
        self.assertEqual(self.storage.next_post(T0)["id"], outcome.post_id)

    def test_repeated_post_is_a_duplicate(self):
        accept(self.storage, incoming(), None, T0)
        self.assertEqual(accept(self.storage, incoming(), None, T0).kind, "duplicate")

    def test_album_waits_a_short_pause(self):
        accept(self.storage, incoming(album="c:g"), "a" * 64, T0)
        self.assertIsNone(self.storage.next_post(T0))
        self.assertIsNotNone(self.storage.next_post(T0 + ALBUM_PAUSE))

    def test_second_part_joins_the_album(self):
        first = accept(self.storage, incoming(album="c:g"), "a" * 64, T0)
        second = accept(self.storage, incoming(key="c:6", message_id=6, album="c:g"), "b" * 64, T0)
        self.assertEqual(second.kind, "joined")
        self.assertEqual(second.post_id, first.post_id)
        self.assertEqual(self.storage.post_images(first.post_id), ["a" * 64, "b" * 64])

    def test_album_part_without_a_key_of_the_post(self):
        outcome = accept(self.storage, incoming(key=None, part_key="chat:9"), None, T0)
        self.assertEqual(self.storage.get_post(outcome.post_id)["origin_key"], None)
        self.assertTrue(self.storage.has_part("chat:9"))


class AnalyzeTest(ServiceCase):
    def test_zone_is_saved_with_the_current_price(self):
        result, post_id = self.analyze(FakeBroker(answer(raw_zone())))
        zone = self.storage.zones_of_post(post_id)[0]
        self.assertEqual(result.kind, "zones")
        self.assertEqual((zone["symbol"], zone["side"], zone["price"]), ("BTCUSDT", "SHORT", 80000))
        self.assertEqual(self.storage.next_post(T0), None)

    def test_post_without_zones_goes_to_the_digest(self):
        result, _ = self.analyze(FakeBroker(answer()))
        self.assertEqual(result.kind, "opinion")
        self.assertEqual(len(self.storage.posts_since(T0, "opinion")), 1)

    def test_image_reaches_the_model(self):
        broker = FakeBroker(answer())
        self.analyze(broker, image="a" * 64)
        self.assertEqual(broker.calls[0][1], "a" * 64)

    def test_edited_text_is_read(self):
        broker = FakeBroker(answer())
        post = accept(self.storage, incoming(), None, T0)
        self.storage.record_edit("c:5", "новый текст", T0)
        asyncio.run(analyze_post(self.storage, broker, FakeMarket(), self.storage.get_post(post.post_id), SYMBOLS, T0))
        self.assertIn("новый текст", broker.calls[0][0])

    def test_model_failure_leaves_the_post_queued(self):
        with self.assertRaises(HermesError):
            self.analyze(FakeBroker(HermesError("BROKER_DOWN")))
        self.assertIsNotNone(self.storage.next_post(T0))

    def test_market_failure_keeps_the_zone(self):
        _, post_id = self.analyze(FakeBroker(answer(raw_zone())), market=FakeMarket(broken=True))
        zone = self.storage.zones_of_post(post_id)[0]
        self.assertIsNone(zone["price"])
        self.assertIn("PRICE_UNAVAILABLE", zone["flags"])

    def test_unknown_coin_does_not_ask_for_a_price(self):
        market = FakeMarket()
        market.price = None
        _, post_id = self.analyze(FakeBroker(answer(raw_zone(symbol="NOSUCHCOIN"))), market=market)
        self.assertIn("UNKNOWN_SYMBOL", self.storage.zones_of_post(post_id)[0]["flags"])

    def test_chart_zone_is_flagged(self):
        _, post_id = self.analyze(FakeBroker(answer(raw_zone())), text="смотрите график", image="a" * 64)
        self.assertIn("CHECK_BY_EYE", self.storage.zones_of_post(post_id)[0]["flags"])

    def test_text_zone_is_plain(self):
        _, post_id = self.analyze(FakeBroker(answer(raw_zone())), image="a" * 64)
        self.assertEqual(self.storage.zones_of_post(post_id)[0]["flags"], [])

    def test_open_zones_of_this_author_reach_the_prompt(self):
        self.analyze(FakeBroker(answer(raw_zone())))
        broker = FakeBroker(answer())
        self.analyze(broker, key="c:6")
        self.assertIn("#1 BTCUSDT SHORT 82000–84000", broker.calls[0][0])

    def test_zones_of_another_author_stay_out_of_the_prompt(self):
        self.analyze(FakeBroker(answer(raw_zone())))
        broker = FakeBroker(answer())
        self.analyze(broker, key="c:6", author="Другой")
        self.assertNotIn("<open_zones>", broker.calls[0][0])


class LinkTest(ServiceCase):
    def zone(self, **changes):
        return parse_answer(answer(raw_zone(**changes))).zones[0]

    OPEN = [{"id": 1, "symbol": "BTCUSDT", "side": "SHORT"}, {"id": 2, "symbol": "BTCUSDT", "side": "LONG"},
            {"id": 3, "symbol": "ETHUSDT", "side": "SHORT"}]

    def test_named_zone_is_taken(self):
        self.assertEqual(find_link(self.OPEN, self.zone(action="cancel", replaces=1))[0]["id"], 1)

    def test_named_zone_of_another_coin_is_refused(self):
        self.assertIsNone(find_link(self.OPEN, self.zone(action="cancel", replaces=3))[0])

    def test_named_zone_of_another_side_is_refused(self):
        self.assertIsNone(find_link(self.OPEN, self.zone(action="cancel", replaces=2))[0])

    def test_named_zone_of_another_author_is_refused(self):
        self.assertIsNone(find_link(self.OPEN, self.zone(action="cancel", replaces=99))[0])

    def test_single_candidate_needs_no_name(self):
        self.assertEqual(find_link(self.OPEN, self.zone(action="cancel"))[0]["id"], 1)

    def test_side_may_be_unknown_for_a_cancel(self):
        zone = self.zone(symbol="ETHUSDT", action="cancel", side=None, zone_low=None, zone_high=None)
        self.assertEqual(find_link(self.OPEN, zone)[0]["id"], 3)

    def test_two_candidates_stay_unlinked(self):
        zone = self.zone(action="cancel", side=None, zone_low=None, zone_high=None)
        self.assertEqual(find_link(self.OPEN, zone), (None, 2))


class ZoneChangeTest(ServiceCase):
    def first(self):
        _, post_id = self.analyze(FakeBroker(answer(raw_zone())))
        return self.storage.zones_of_post(post_id)[0]

    def test_cancel_closes_the_author_zone(self):
        zone = self.first()
        self.analyze(FakeBroker(answer(raw_zone(action="cancel", replaces=zone["id"]))), key="c:6", text="отмена")
        self.assertEqual(self.storage.get_zone(zone["id"])["status"], "CANCELLED")

    def test_result_closes_the_author_zone(self):
        zone = self.first()
        self.analyze(FakeBroker(answer(raw_zone(action="result"))), key="c:6", text="дошли")
        self.assertEqual(self.storage.get_zone(zone["id"])["status"], "RESULT")

    def test_update_changes_the_zone_and_adds_no_new_one(self):
        zone = self.first()
        result, post_id = self.analyze(FakeBroker(answer(raw_zone(action="update", zone_low=83000, zone_high=85000))),
                                       key="c:6", text="сдвигаю 83000-85000")
        self.assertEqual(self.storage.get_zone(zone["id"])["version"], 2)
        self.assertEqual(self.storage.zones_of_post(post_id), [])
        self.assertEqual(result.kind, "zones")

    def test_other_author_cannot_touch_the_zone(self):
        zone = self.first()
        self.analyze(FakeBroker(answer(raw_zone(action="cancel", replaces=zone["id"]))), key="c:6", author="Другой")
        self.assertEqual(self.storage.get_zone(zone["id"])["status"], "WATCHING")

    def test_update_without_a_zone_becomes_a_new_one(self):
        result, post_id = self.analyze(FakeBroker(answer(raw_zone(action="update"))))
        self.assertEqual(len(self.storage.zones_of_post(post_id)), 1)
        self.assertEqual(result.kind, "zones")

    def test_cancel_without_a_zone_changes_nothing(self):
        result, _ = self.analyze(FakeBroker(answer(raw_zone(action="cancel"))))
        self.assertEqual(result.kind, "opinion")
        self.assertEqual(result.unclear, ())

    def test_unclear_cancel_is_reported(self):
        self.analyze(FakeBroker(answer(raw_zone(), raw_zone(side="LONG"))))
        result, _ = self.analyze(FakeBroker(answer(raw_zone(action="cancel", side=None, zone_low=None, zone_high=None))),
                                 key="c:6", text="отмена")
        self.assertEqual(result.unclear[0].symbol, "BTCUSDT")


class NextTryTest(unittest.TestCase):
    def post(self, attempts=0):
        return {"attempts": attempts}

    def test_outage_is_retried_later(self):
        self.assertEqual(next_try(self.post(), HermesError("BROKER_DOWN"), T0), T0 + RETRY_EVERY)

    def test_pause_grows(self):
        self.assertEqual(next_try(self.post(2), HermesError("BROKER_DOWN"), T0), T0 + 4 * RETRY_EVERY)

    def test_pause_has_a_ceiling(self):
        self.assertEqual(next_try(self.post(20), HermesError("PAUSED"), T0), T0 + MAX_RETRY)

    def test_outage_never_gives_up(self):
        self.assertIsNotNone(next_try(self.post(500), HermesError("DAILY_LIMIT"), T0))

    def test_bad_answers_end_after_three_tries(self):
        self.assertIsNotNone(next_try(self.post(1), BadAnswer("INVALID_JSON"), T0))
        self.assertIsNone(next_try(self.post(2), BadAnswer("INVALID_JSON"), T0))

    def test_rejected_image_ends_after_three_tries(self):
        self.assertIsNone(next_try(self.post(2), HermesError("IMAGE_REJECTED"), T0))

    def test_unexpected_bug_ends_after_three_tries(self):
        self.assertIsNone(next_try(self.post(2), ValueError("bug"), T0))


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
        post_id = self.storage.add_post("Илья", None, None, "текст", "idea", T0)
        return self.storage.add_idea(post_id, "BTCUSDT", "LONG", 80000.0, 80000.0, 78000.0, [84000.0], False, 80000.0,
                                     [], T0)

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
