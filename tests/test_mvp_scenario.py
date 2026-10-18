import unittest
from datetime import datetime, timedelta, timezone

from scripts.mvp_scenario import Candle, analyze


def candles():
    start = datetime(2026, 10, 12, tzinfo=timezone.utc)
    values = [
        (100, 102, 99, 101), (101, 104, 100, 103), (103, 108, 102, 107),
        (107, 111, 106, 110), (110, 110, 105, 106), (106, 107, 103, 104),
        (104, 106, 102, 103), (103, 105, 101, 104), (104, 106, 103, 105),
        (105, 107, 104, 106),
    ]
    return [Candle(start + timedelta(hours=i), *v) for i, v in enumerate(values)]


class ScenarioTests(unittest.TestCase):
    def test_long_confluence_yields_levels_only_from_closed_candles(self):
        rows = candles()
        # Explicit anchor indices, never chosen using candles after the snapshot.
        result = analyze(rows, as_of=rows[-1].start + timedelta(hours=1),
                         interval=timedelta(hours=1), low_anchor=0, high_anchor=3,
                         zone_candle_index=8, min_rr=1.0, tick_size=0.1)
        self.assertEqual(result["direction"], "LONG")
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertGreater(result["rr"], 1)
        self.assertLess(result["sl"], result["entry"])
        self.assertGreater(result["tp"], result["entry"])
        for key in ("entry", "sl", "tp"):
            self.assertAlmostEqual(result[key] / 0.1, round(result[key] / 0.1))

    def test_unclosed_last_candle_cannot_influence_scenario(self):
        rows = candles()
        unfinished = Candle(rows[-1].start + timedelta(hours=1), 1000, 2000, 1, 1999)
        as_of = unfinished.start + timedelta(minutes=10)
        base = analyze(rows, as_of=as_of, interval=timedelta(hours=1),
                       low_anchor=0, high_anchor=3, zone_candle_index=8, min_rr=1.0)
        with_unfinished = analyze([*rows, unfinished], as_of=as_of,
                                  interval=timedelta(hours=1), low_anchor=0,
                                  high_anchor=3, zone_candle_index=8, min_rr=1.0)
        self.assertEqual(base, with_unfinished)

    def test_no_overlap_refuses_entry(self):
        rows = candles()
        result = analyze(rows, as_of=rows[-1].start + timedelta(hours=1),
                         interval=timedelta(hours=1), low_anchor=0, high_anchor=1,
                         zone_candle_index=8, min_rr=1.0)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIsNone(result["entry"])

    def test_future_anchor_is_rejected(self):
        rows = candles()
        result = analyze(rows, as_of=rows[3].start + timedelta(hours=1),
                         interval=timedelta(hours=1), low_anchor=0, high_anchor=8,
                         zone_candle_index=3)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIsNone(result["entry"])

    def test_stale_price_refuses_levels(self):
        rows = candles()
        rows.append(Candle(rows[-1].start + timedelta(hours=1), 105, 150, 104, 149))
        result = analyze(rows, as_of=rows[-1].start + timedelta(hours=1),
                         interval=timedelta(hours=1), low_anchor=0, high_anchor=3,
                         zone_candle_index=8, min_rr=1.0)
        self.assertEqual(result["reason"], "price no longer near entry")
        self.assertIsNone(result["entry"])

    def test_zone_before_second_anchor_is_rejected(self):
        rows = candles()
        result = analyze(rows, as_of=rows[-1].start + timedelta(hours=1),
                         interval=timedelta(hours=1), low_anchor=0, high_anchor=3,
                         zone_candle_index=2)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIsNone(result["entry"])


if __name__ == "__main__":
    unittest.main()
