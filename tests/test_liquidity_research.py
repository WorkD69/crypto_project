import unittest
from datetime import datetime, timedelta, timezone

from scripts.mvp_scenario import Candle
from scripts.liquidity_research import find_observation, validate_snapshot


def bars(lows, highs=None):
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    highs = highs or [x + 10 for x in lows]
    return [Candle(start + timedelta(hours=4 * i), x + 3, h, x, x + 4)
            for i, (x, h) in enumerate(zip(lows, highs))]


class LiquidityResearchTests(unittest.TestCase):
    def test_prior_equal_lows_create_watch_zone_without_trade_levels(self):
        rows = bars([109, 106, 103, 100, 104, 106, 108, 103, 100.1, 104, 107, 110])
        result = find_observation(rows, as_of=rows[-1].start + timedelta(hours=4),
                                  interval=timedelta(hours=4), tolerance=.002)
        self.assertEqual(result["status"], "WATCHING")
        self.assertEqual(result["direction"], "LONG")
        self.assertEqual(result["zone_kind"], "equal_lows")
        self.assertEqual(result["zone"], [100, 100.1])
        self.assertIsNone(result["entry"])
        self.assertIsNone(result["sl"])
        self.assertIsNone(result["rr"])

    def test_future_unclosed_bar_cannot_create_zone(self):
        rows = bars([109, 106, 103, 100, 104, 106, 108, 103, 100.1, 104, 107, 110])
        as_of = rows[-1].start + timedelta(hours=4)
        base = find_observation(rows, as_of=as_of, interval=timedelta(hours=4))
        unfinished = Candle(as_of, 110, 120, 80, 90)
        self.assertEqual(base, find_observation(rows + [unfinished], as_of=as_of,
                                                interval=timedelta(hours=4)))

    def test_invalid_snapshot_rejects_nonchronological_or_bad_ohlc(self):
        rows = bars([100, 101, 102])
        with self.assertRaises(ValueError):
            validate_snapshot([rows[1], rows[0]])
        with self.assertRaises(ValueError):
            validate_snapshot([Candle(rows[0].start, 100, 99, 98, 100)])


if __name__ == "__main__":
    unittest.main()
