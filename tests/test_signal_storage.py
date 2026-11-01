import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from signal_bot.storage import Storage

T0 = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def idea_fields(**overrides):
    fields = dict(post_id=1, symbol="BTCUSDT", side="LONG", entry_low=100.0, entry_high=102.0, stop=95.0,
                  targets=[112.0, 120.0], entry_assumed=False, price=103.0, warnings=["X"], created_at=T0)
    return {**fields, **overrides}


class StorageCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = Storage(Path(self.directory.name) / "bot.sqlite3")

    def post(self, author="Автор", key=None, kind="idea", at=T0):
        return self.storage.add_post(author, key, None, "текст", kind, at)


class OwnerTest(StorageCase):
    def test_first_user_becomes_owner(self):
        self.assertTrue(self.storage.claim_owner(7))
        self.assertEqual(self.storage.owner_id(), 7)

    def test_second_user_cannot_take_over(self):
        self.storage.claim_owner(7)
        self.assertFalse(self.storage.claim_owner(8))
        self.assertEqual(self.storage.owner_id(), 7)

    def test_owner_can_reclaim(self):
        self.storage.claim_owner(7)
        self.assertTrue(self.storage.claim_owner(7))


class PostTest(StorageCase):
    def test_same_origin_is_duplicate(self):
        self.assertIsNotNone(self.post(key="c1:5"))
        self.assertIsNone(self.post(key="c1:5"))

    def test_posts_without_origin_are_never_duplicates(self):
        self.assertIsNotNone(self.post())
        self.assertIsNotNone(self.post())

    def test_posts_since_filters_by_time_and_kind(self):
        self.post(kind="opinion", at=T0 - timedelta(days=2))
        self.post(kind="opinion", at=T0)
        self.post(kind="idea", at=T0)
        self.assertEqual(len(self.storage.posts_since(T0 - timedelta(hours=1), "opinion")), 1)


class IdeaTest(StorageCase):
    def setUp(self):
        super().setUp()
        self.post()

    def test_saved_idea_keeps_levels(self):
        idea = self.storage.get_idea(self.storage.add_idea(**idea_fields()))
        self.assertEqual(idea["targets"], [112.0, 120.0])
        self.assertEqual(idea["warnings"], ["X"])
        self.assertEqual(idea["status"], "WAITING")

    def test_first_decision_wins(self):
        idea_id = self.storage.add_idea(**idea_fields())
        self.assertTrue(self.storage.set_decision(idea_id, "take"))
        self.assertFalse(self.storage.set_decision(idea_id, "skip"))
        self.assertEqual(self.storage.get_idea(idea_id)["decision"], "take")

    def test_trade_round_trip_uses_first_target(self):
        idea_id = self.storage.add_idea(**idea_fields())
        trade = self.storage.trade(self.storage.get_idea(idea_id))
        self.assertEqual(trade.target, 112.0)
        self.assertEqual(trade.created_at, T0)
        changed = replace(trade, status="ENTERED", fill_price=101.0, last_bar=T0)
        self.storage.save_trade(idea_id, changed)
        self.assertEqual(self.storage.trade(self.storage.get_idea(idea_id)), changed)

    def test_open_ideas_exclude_finished(self):
        first = self.storage.add_idea(**idea_fields())
        second = self.storage.add_idea(**idea_fields())
        trade = self.storage.trade(self.storage.get_idea(first))
        self.storage.save_trade(first, replace(trade, status="SL", result_r=-1.0))
        self.assertEqual([i["id"] for i in self.storage.open_ideas()], [second])

    def test_card_message_id_is_stored(self):
        idea_id = self.storage.add_idea(**idea_fields())
        self.storage.set_card_message(idea_id, 55)
        self.assertEqual(self.storage.get_idea(idea_id)["card_message_id"], 55)


    def test_idea_can_start_untracked(self):
        idea_id = self.storage.add_idea(**idea_fields(stop=None), status="NOT_TRACKED")
        self.assertEqual(self.storage.get_idea(idea_id)["status"], "NOT_TRACKED")
        self.assertEqual(self.storage.open_ideas(), [])

    def test_idea_includes_post_text(self):
        idea_id = self.storage.add_idea(**idea_fields())
        self.assertEqual(self.storage.get_idea(idea_id)["text"], "текст")


class PostChangeTest(StorageCase):
    def test_edit_keeps_original_and_returns_idea(self):
        post_id = self.post(key="c1:5")
        idea_id = self.storage.add_idea(**idea_fields(post_id=post_id))
        self.assertEqual(self.storage.record_edit("c1:5", "новый", T0)["idea"]["id"], idea_id)
        self.assertEqual(self.storage.get_idea(idea_id)["text"], "текст")
        self.assertEqual(self.storage.get_idea(idea_id)["edited_text"], "новый")

    def test_same_text_is_not_an_edit(self):
        post_id = self.post(key="c1:5")
        self.storage.add_idea(**idea_fields(post_id=post_id))
        self.assertIsNone(self.storage.record_edit("c1:5", "текст", T0))

    def test_repeated_edit_text_is_not_a_new_edit(self):
        post_id = self.post(key="c1:5")
        self.storage.add_idea(**idea_fields(post_id=post_id))
        self.storage.record_edit("c1:5", "новый", T0)
        self.assertIsNone(self.storage.record_edit("c1:5", "новый", T0))

    def test_edit_of_opinion_returns_nothing(self):
        self.post(key="c1:6", kind="opinion")
        self.assertIsNone(self.storage.record_edit("c1:6", "новый", T0))

    def test_deleted_idea_stays_in_stats(self):
        post_id = self.post(author="Илья", key="c1:5")
        idea_id = self.storage.add_idea(**idea_fields(post_id=post_id))
        self.assertEqual(self.storage.record_delete("c1:5", T0)["idea"]["id"], idea_id)
        row = self.storage.stats()[0]
        self.assertEqual((row["ideas"], row["deleted"]), (1, 1))

    def test_unknown_post_delete_returns_nothing(self):
        self.assertIsNone(self.storage.record_delete("c1:404", T0))


class MigrationTest(unittest.TestCase):
    def test_old_database_keeps_its_posts_out_of_the_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bot.sqlite3"
            old = sqlite3.connect(path)
            old.executescript("""
                CREATE TABLE posts (id INTEGER PRIMARY KEY, author TEXT NOT NULL, origin_key TEXT UNIQUE, url TEXT,
                    text TEXT NOT NULL, kind TEXT NOT NULL, received_at TEXT NOT NULL,
                    edited_text TEXT, edited_at TEXT, deleted_at TEXT);
                INSERT INTO posts(author,origin_key,text,kind,received_at) VALUES('a','c:1','t','opinion','2026-11-01');
            """)
            old.close()
            storage = Storage(path)
            self.assertIsNone(storage.next_post(T0))
            self.assertTrue(storage.has_part("c:1"))
            self.assertEqual(len(storage.posts_since(T0 - timedelta(days=30), "opinion")), 1)


def part(key="c:5", album=None, text="текст", message_id=5, image=None, **overrides):
    fields = dict(author="Автор", origin_key=key, url=None, text=text, posted_at=T0, album=album, part_key=key,
                  message_id=message_id, image=image, file_id=None, reply_to=None, ready_at=T0, received_at=T0)
    return {**fields, **overrides}


def zone_row(**overrides):
    fields = dict(symbol="BTCUSDT", side="SHORT", zone_low=82000.0, zone_high=84000.0, stop=None, targets=[78000.0],
                  timeframe="4H", trigger="zone", confidence=0.8, evidence="рамка", chart_price=80000.0, flags=[],
                  image="a" * 64, price=80100.0)
    return {**fields, **overrides}


class QueueTest(StorageCase):
    def test_single_post_is_queued(self):
        post_id, created = self.storage.add_part(**part())
        self.assertTrue(created)
        post = self.storage.next_post(T0)
        self.assertEqual(post["id"], post_id)
        self.assertEqual(post["kind"], "pending")

    def test_repeated_part_is_refused(self):
        self.storage.add_part(**part())
        self.assertIsNone(self.storage.add_part(**part()))

    def test_part_seen_as_a_post_key_is_known(self):
        self.storage.add_part(**part(key="c:5"))
        self.assertTrue(self.storage.has_part("c:5"))
        self.assertFalse(self.storage.has_part("c:6"))

    def test_album_parts_join_one_post(self):
        first, created = self.storage.add_part(**part(key="c:5", album="c:g", text="", image="a" * 64))
        second, joined = self.storage.add_part(**part(key="c:6", album="c:g", text="подпись", message_id=6,
                                                      image="b" * 64))
        self.assertEqual(first, second)
        self.assertEqual((created, joined), (True, False))
        self.assertEqual(self.storage.next_post(T0)["text"], "подпись")

    def test_album_images_keep_message_order(self):
        self.storage.add_part(**part(key="c:6", album="c:g", message_id=6, image="b" * 64))
        post_id, _ = self.storage.add_part(**part(key="c:5", album="c:g", message_id=5, image="a" * 64))
        self.assertEqual(self.storage.post_images(post_id), ["a" * 64, "b" * 64])

    def test_album_waits_for_its_last_part(self):
        self.storage.add_part(**part(key="c:5", album="c:g", ready_at=T0))
        self.storage.add_part(**part(key="c:6", album="c:g", ready_at=T0 + timedelta(seconds=5)))
        self.assertIsNone(self.storage.next_post(T0 + timedelta(seconds=4)))
        self.assertIsNotNone(self.storage.next_post(T0 + timedelta(seconds=5)))

    def test_late_part_of_a_parsed_album_stays_a_new_post(self):
        first, _ = self.storage.add_part(**part(key="c:5", album="c:g"))
        self.storage.save_parse(first, "opinion", [], T0)
        late, created = self.storage.add_part(**part(key="c:6", album="c:g"))
        self.assertNotEqual(first, late)
        self.assertTrue(created)

    def test_fresh_post_goes_before_a_retried_one(self):
        old, _ = self.storage.add_part(**part(key="c:5"))
        self.storage.postpone_post(old, T0, "BROKER_DOWN")
        new, _ = self.storage.add_part(**part(key="c:6"))
        self.assertEqual(self.storage.next_post(T0)["id"], new)

    def test_postponed_post_waits_for_its_time(self):
        post_id, _ = self.storage.add_part(**part())
        self.storage.postpone_post(post_id, T0 + timedelta(minutes=5), "BROKER_DOWN")
        self.assertIsNone(self.storage.next_post(T0))
        post = self.storage.next_post(T0 + timedelta(minutes=5))
        self.assertEqual((post["attempts"], post["last_error"]), (1, "BROKER_DOWN"))

    def test_given_up_post_leaves_the_queue_and_stays_stored(self):
        post_id, _ = self.storage.add_part(**part())
        self.storage.give_up_post(post_id, "INVALID_JSON")
        self.assertIsNone(self.storage.next_post(T0))
        self.assertEqual(self.storage.get_post(post_id)["kind"], "failed")

    def test_notice_is_remembered(self):
        post_id, _ = self.storage.add_part(**part())
        self.storage.set_notice(post_id, 77)
        self.assertEqual(self.storage.get_post(post_id)["notice_id"], 77)

    def test_waiting_images_are_known(self):
        first, _ = self.storage.add_part(**part(key="c:5", image="a" * 64))
        self.storage.add_part(**part(key="c:6", image="b" * 64))
        self.storage.save_parse(first, "opinion", [], T0)
        self.assertEqual(self.storage.waiting_images(), {"b" * 64})

    def test_pending_posts_are_counted_for_the_digest(self):
        self.storage.add_part(**part())
        self.assertEqual(self.storage.count_unparsed(), 1)

    def test_edited_text_of_a_queued_post_is_used(self):
        self.storage.add_part(**part())
        self.storage.record_edit("c:5", "новый", T0)
        self.assertEqual(self.storage.next_post(T0)["edited_text"], "новый")


class ZoneStoreCase(StorageCase):
    def setUp(self):
        super().setUp()
        self.post_id, _ = self.storage.add_part(**part())

    def add(self, post_id=None, **overrides):
        self.storage.save_parse(post_id or self.post_id, "zones", [("new", zone_row(**overrides), None)], T0)
        return self.storage.zones_of_post(post_id or self.post_id)[-1]


class ZoneTest(ZoneStoreCase):
    def test_saved_zone_keeps_its_fields(self):
        zone = self.add()
        self.assertEqual(zone["targets"], [78000.0])
        self.assertEqual((zone["status"], zone["version"], zone["author"]), ("WATCHING", 1, "Автор"))
        self.assertEqual(zone["image"], "a" * 64)
        self.assertEqual(self.storage.get_post(self.post_id)["kind"], "zones")

    def test_new_zone_writes_an_event_nobody_is_told_about(self):
        zone = self.add()
        self.assertEqual(self.storage.zone_events(zone["id"])[0]["kind"], "new")
        self.assertEqual(self.storage.unsent_events(), [])

    def test_new_zone_waits_for_its_card(self):
        zone = self.add()
        self.assertEqual([item["id"] for item in self.storage.unsent_zones()], [zone["id"]])
        self.storage.set_zone_card(zone["id"], 55)
        self.assertEqual(self.storage.unsent_zones(), [])
        self.assertEqual(self.storage.get_zone(zone["id"])["card_message_id"], 55)

    def test_open_zones_are_filtered_by_author(self):
        zone = self.add()
        self.assertEqual([item["id"] for item in self.storage.open_zones("Автор")], [zone["id"]])
        self.assertEqual(self.storage.open_zones("Другой"), [])

    def test_all_open_zones(self):
        self.add()
        self.assertEqual(len(self.storage.open_zones()), 1)

    def test_first_decision_wins(self):
        zone = self.add()
        self.assertTrue(self.storage.set_zone_decision(zone["id"], "take"))
        self.assertFalse(self.storage.set_zone_decision(zone["id"], "skip"))
        self.assertEqual(self.storage.get_zone(zone["id"])["decision"], "take")

    def test_zones_since(self):
        self.add()
        self.assertEqual(len(self.storage.zones_since(T0)), 1)
        self.assertEqual(self.storage.zones_since(T0 + timedelta(hours=1)), [])


class ZoneLinkTest(ZoneStoreCase):
    def link(self, action, zone, fields=None):
        post_id, _ = self.storage.add_part(**part(key="c:9"))
        self.storage.save_parse(post_id, "zones", [(action, fields or {}, zone["id"])], T0 + timedelta(hours=1))
        return self.storage.get_zone(zone["id"]), post_id

    def test_update_raises_version_and_changes_levels(self):
        zone = self.add()
        updated, post_id = self.link("update", zone, {"zone_low": 83000.0, "zone_high": 85000.0, "stop": 86000.0,
                                                      "targets": [], "flags": ["X"]})
        self.assertEqual((updated["zone_low"], updated["zone_high"], updated["stop"]), (83000, 85000, 86000))
        self.assertEqual((updated["version"], updated["status"]), (2, "WATCHING"))
        self.assertEqual(updated["post_id"], self.post_id)
        self.assertEqual(self.storage.unsent_events()[0]["post_id"], post_id)

    def test_update_keeps_levels_it_does_not_repeat(self):
        zone = self.add(stop=85500.0)
        updated, _ = self.link("update", zone, {"zone_low": None, "zone_high": None, "stop": None, "targets": []})
        self.assertEqual((updated["zone_low"], updated["stop"], updated["targets"]), (82000, 85500, [78000.0]))

    def test_update_event_remembers_old_levels(self):
        zone = self.add()
        self.link("update", zone, {"zone_low": 83000.0, "zone_high": 85000.0, "stop": None, "targets": []})
        self.assertEqual(self.storage.unsent_events()[0]["before"]["zone_low"], 82000)

    def test_cancel_closes_the_zone(self):
        zone = self.add()
        updated, _ = self.link("cancel", zone)
        self.assertEqual(updated["status"], "CANCELLED")
        self.assertEqual(self.storage.open_zones(), [])
        self.assertEqual(self.storage.unsent_events()[0]["kind"], "cancel")

    def test_result_closes_the_zone(self):
        self.assertEqual(self.link("result", self.add())[0]["status"], "RESULT")

    def test_event_is_told_once(self):
        self.link("cancel", self.add())
        event = self.storage.unsent_events()[0]
        self.storage.event_sent(event["id"], T0)
        self.assertEqual(self.storage.unsent_events(), [])


class PostChangeOfZonesTest(ZoneStoreCase):
    def test_edit_reports_zones(self):
        zone = self.add()
        self.assertEqual([item["id"] for item in self.storage.record_edit("c:5", "новый", T0)["zones"]], [zone["id"]])

    def test_delete_reports_zones(self):
        zone = self.add()
        self.assertEqual([item["id"] for item in self.storage.record_delete("c:5", T0)["zones"]], [zone["id"]])

    def test_deleted_flag_reaches_the_zone(self):
        zone = self.add()
        self.storage.record_delete("c:5", T0)
        self.assertIsNotNone(self.storage.get_zone(zone["id"])["deleted_at"])


class ZoneFixTest(ZoneStoreCase):
    def test_fix_changes_levels_and_keeps_the_raw_text(self):
        zone = self.add()
        self.storage.fix_zone(zone["id"], {"zone_low": 81000.0, "zone_high": 83000.0}, "81000-83000", T0)
        fixed = self.storage.get_zone(zone["id"])
        self.assertEqual((fixed["zone_low"], fixed["zone_high"], fixed["version"]), (81000, 83000, 2))
        self.assertIn("OWNER_FIX", fixed["flags"])
        event = self.storage.zone_events(zone["id"])[-1]
        self.assertEqual((event["kind"], event["note"]), ("fix", "81000-83000"))
        self.assertEqual(event["before"]["zone_low"], 82000)

    def test_fix_takes_away_the_check_by_eye_note(self):
        zone = self.add(flags=["CHART_ONLY", "CHECK_BY_EYE"])
        self.storage.fix_zone(zone["id"], {"stop": 85000.0}, "стоп 85000", T0)
        self.assertEqual(self.storage.get_zone(zone["id"])["flags"], ["OWNER_FIX"])

    def test_no_zone_at_all_rejects_it(self):
        zone = self.add()
        self.storage.reject_zone(zone["id"], "нет", T0)
        self.assertEqual(self.storage.get_zone(zone["id"])["status"], "REJECTED")
        self.assertEqual(self.storage.open_zones(), [])
        self.assertEqual(self.storage.zone_events(zone["id"])[-1]["kind"], "reject")

    def test_fix_is_not_told_as_an_author_event(self):
        zone = self.add()
        self.storage.fix_zone(zone["id"], {"stop": 85000.0}, "стоп 85000", T0)
        self.assertEqual(self.storage.unsent_events(), [])


class WaitingFixTest(StorageCase):
    def test_waiting_zone_round_trip(self):
        self.assertIsNone(self.storage.waiting_fix())
        self.storage.set_waiting_fix(12)
        self.assertEqual(self.storage.waiting_fix(), 12)
        self.storage.set_waiting_fix(None)
        self.assertIsNone(self.storage.waiting_fix())


class CursorTest(StorageCase):
    def test_cursor_round_trip(self):
        self.assertIsNone(self.storage.cursor(10))
        self.storage.set_cursor(10, 55)
        self.assertEqual(self.storage.cursor(10), 55)

    def test_cursor_never_moves_back(self):
        self.storage.set_cursor(10, 55)
        self.storage.set_cursor(10, 50)
        self.assertEqual(self.storage.cursor(10), 55)


class StatsTest(StorageCase):
    def closed(self, author, status, result, decision=None):
        post = self.post(author=author)
        idea_id = self.storage.add_idea(**idea_fields(post_id=post))
        if decision:
            self.storage.set_decision(idea_id, decision)
        trade = self.storage.trade(self.storage.get_idea(idea_id))
        self.storage.save_trade(idea_id, replace(trade, status=status, result_r=result))

    def test_stats_group_by_author(self):
        self.closed("Илья", "TP", 2.0, "take")
        self.closed("Илья", "SL", -1.0, "skip")
        self.closed("Вайкофф", "MISSED", None)
        rows = {row["author"]: row for row in self.storage.stats()}
        self.assertEqual(rows["Илья"]["ideas"], 2)
        self.assertEqual(rows["Илья"]["closed"], 2)
        self.assertEqual(rows["Илья"]["wins"], 1)
        self.assertAlmostEqual(rows["Илья"]["total_r"], 1.0)
        self.assertAlmostEqual(rows["Илья"]["taken_r"], 2.0)
        self.assertEqual(rows["Вайкофф"]["closed"], 0)


if __name__ == "__main__":
    unittest.main()
