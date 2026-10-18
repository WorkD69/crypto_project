import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.idea_journal import IdeaJournal
from scripts.mvp_scenario import Candle


T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def idea():
    return {"source": "https://telegra.ph/example", "created_at": T0.isoformat(),
            "symbol": "BTCUSDT", "timeframe": "4H", "direction": "LONG",
            "idea_type": "observation", "factors": [{"kind": "prior_low", "origin": "4H candle"}],
            "entry_zone": [100, 101], "sl": None, "targets": [], "rr": None,
            "valid_until": (T0 + timedelta(days=3)).isoformat(),
            "rule_version": "liquidity-watch-0.2", "chart_path": "chart.png"}


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal = IdeaJournal(Path(self.temp.name) / "ideas.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_immutable_and_touch_is_not_fill(self):
        original = idea()
        idea_id = self.journal.create(original)
        original["entry_zone"][0] = 1
        rows = [Candle(T0 + timedelta(hours=4), 106, 108, 100.5, 105)]
        self.journal.check_candles(idea_id, rows, as_of=T0 + timedelta(hours=8),
                                   interval=timedelta(hours=4))
        record = self.journal.get(idea_id)
        self.assertEqual(record["snapshot"]["entry_zone"], [100, 101])
        self.assertEqual(record["status"], "TOUCHED")
        self.assertIsNone(record["snapshot"]["sl"])
        self.assertIn("TOUCHED", [e["kind"] for e in self.journal.events(idea_id)])

    def test_assumed_fill_then_tp_records_r(self):
        payload = idea()
        payload.update({"idea_type": "scenario", "entry_zone": [100, 100],
                        "sl": 95, "targets": [110], "rr": 2.0})
        idea_id = self.journal.create(payload)
        self.journal.assume_fill(idea_id, at=T0 + timedelta(hours=4), price=100)
        self.journal.check_candles(idea_id,
            [Candle(T0 + timedelta(hours=8), 102, 111, 99, 110)],
            as_of=T0 + timedelta(hours=12), interval=timedelta(hours=4))
        self.assertEqual(self.journal.get(idea_id)["status"], "TP")
        self.assertEqual(self.journal.get(idea_id)["result_r"], 2.0)

    def test_simultaneous_sl_and_tp_is_ambiguous(self):
        payload = idea()
        payload.update({"idea_type": "scenario", "entry_zone": [100, 100],
                        "sl": 95, "targets": [110], "rr": 2.0})
        idea_id = self.journal.create(payload)
        self.journal.assume_fill(idea_id, at=T0 + timedelta(hours=4), price=100)
        self.journal.check_candles(idea_id,
            [Candle(T0 + timedelta(hours=8), 102, 111, 94, 100)],
            as_of=T0 + timedelta(hours=12), interval=timedelta(hours=4))
        self.assertEqual(self.journal.get(idea_id)["status"], "AMBIGUOUS")
        self.assertIsNone(self.journal.get(idea_id)["result_r"])

    def test_expiry_and_no_future_candles(self):
        idea_id = self.journal.create(idea())
        self.journal.check_candles(idea_id,
            [Candle(T0 + timedelta(days=4), 100, 102, 99, 101)],
            as_of=T0 + timedelta(days=4, minutes=1), interval=timedelta(hours=4))
        self.assertEqual(self.journal.get(idea_id)["status"], "EXPIRED")

    def test_publication_metadata_preserves_waiting_state(self):
        idea_id = self.journal.create(idea())
        self.journal.add_event(idea_id, "PUBLISHED", at=T0 + timedelta(minutes=1),
                               message_id="123", url=None)
        record = self.journal.get(idea_id)
        self.assertEqual(record["status"], "WAITING")
        self.assertEqual(record["telegram_message_id"], "123")

    def test_pinned_chart_does_not_change_idea_snapshot(self):
        idea_id = self.journal.create(idea())
        self.journal.add_event(idea_id, "CHART_PINNED", at=T0 + timedelta(minutes=1),
                               path="chart-unique.png", sha256="example")
        record = self.journal.get(idea_id)
        self.assertEqual(record["status"], "WAITING")
        self.assertEqual(record["snapshot"]["chart_path"], "chart.png")
        self.assertEqual(record["chart_path"], "chart-unique.png")

    def test_manual_completion_requires_fill_and_computes_r(self):
        payload = idea()
        payload.update({"idea_type": "scenario", "entry_zone": [100, 100],
                        "sl": 95, "targets": [110], "rr": 2.0})
        idea_id = self.journal.create(payload)
        with self.assertRaises(ValueError):
            self.journal.complete(idea_id, at=T0 + timedelta(hours=4), exit_price=105)
        self.journal.assume_fill(idea_id, at=T0 + timedelta(hours=4), price=100)
        self.journal.complete(idea_id, at=T0 + timedelta(hours=8), exit_price=107.5)
        self.assertEqual(self.journal.get(idea_id)["result_r"], 1.5)
        self.assertEqual(self.journal.get(idea_id)["status"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
