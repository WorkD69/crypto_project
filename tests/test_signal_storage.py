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
        self.assertEqual(self.storage.record_edit("c1:5", "новый", T0)["id"], idea_id)
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
        self.assertEqual(self.storage.record_delete("c1:5", T0)["id"], idea_id)
        row = self.storage.stats()[0]
        self.assertEqual((row["ideas"], row["deleted"]), (1, 1))

    def test_unknown_post_delete_returns_nothing(self):
        self.assertIsNone(self.storage.record_delete("c1:404", T0))


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
