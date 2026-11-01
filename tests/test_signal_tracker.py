import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from signal_bot.tracker import Bar, Trade, advance, reward_risk

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)


def bar(index, high, low, close=None):
    close = low if close is None else close
    return Bar(T0 + index * STEP, close, high, low, close)


LONG = Trade(side="LONG", entry_low=100, entry_high=102, stop=95, target=112, created_at=T0)
SHORT = Trade(side="SHORT", entry_low=100, entry_high=100, stop=105, target=90, created_at=T0)


class EntryTest(unittest.TestCase):
    def test_waits_while_price_is_above_zone(self):
        self.assertEqual(advance(LONG, [bar(0, 106, 104)]).status, "WAITING")

    def test_touching_zone_opens_trade_at_its_middle(self):
        trade = advance(LONG, [bar(0, 106, 101.5)])
        self.assertEqual(trade.status, "ENTERED")
        self.assertEqual(trade.fill_price, 101)

    def test_short_entry(self):
        self.assertEqual(advance(SHORT, [bar(0, 100.5, 98)]).status, "ENTERED")

    def test_target_before_entry_is_missed(self):
        trade = replace(LONG, entry_low=100, entry_high=100, created_at=T0)
        self.assertEqual(advance(trade, [bar(0, 113, 106)]).status, "MISSED")

    def test_waiting_idea_expires(self):
        late = T0 + timedelta(days=14)
        self.assertEqual(advance(LONG, [Bar(late, 106, 107, 105, 106)]).status, "EXPIRED")


class ExitTest(unittest.TestCase):
    def entered(self):
        return advance(LONG, [bar(0, 106, 101.5)])

    def test_stop_gives_minus_one_r_and_fees(self):
        trade = advance(self.entered(), [bar(1, 101, 94)])
        self.assertEqual(trade.status, "SL")
        self.assertLess(trade.result_r, -1)
        self.assertGreater(trade.result_r, -1.05)

    def test_target_gives_planned_r_minus_fees(self):
        trade = advance(self.entered(), [bar(1, 113, 100)])
        self.assertEqual(trade.status, "TP")
        self.assertLess(trade.result_r, 11 / 6)
        self.assertGreater(trade.result_r, 11 / 6 - 0.05)

    def test_short_target_is_positive(self):
        trade = advance(advance(SHORT, [bar(0, 100.5, 99)]), [bar(1, 99, 89)])
        self.assertEqual(trade.status, "TP")
        self.assertGreater(trade.result_r, 1.9)

    def test_both_levels_in_one_candle_count_as_stop(self):
        trade = advance(self.entered(), [bar(1, 113, 94)])
        self.assertEqual(trade.status, "SL")
        self.assertTrue(trade.ambiguous)

    def test_stop_in_entry_candle_counts(self):
        self.assertEqual(advance(LONG, [bar(0, 106, 94)]).status, "SL")

    def test_target_in_entry_candle_is_not_counted(self):
        self.assertEqual(advance(LONG, [bar(0, 113, 101)]).status, "ENTERED")

    def test_open_trade_closes_by_time(self):
        late = T0 + timedelta(days=14)
        trade = advance(self.entered(), [Bar(late, 104, 105, 103, 104)])
        self.assertEqual(trade.status, "TIMEOUT")
        self.assertGreater(trade.result_r, 0)
        self.assertEqual(trade.exit_price, 104)

    def test_closed_trade_ignores_new_candles(self):
        closed = advance(self.entered(), [bar(1, 101, 94)])
        self.assertEqual(advance(closed, [bar(2, 113, 100)]).status, "SL")


class CandleScopeTest(unittest.TestCase):
    def test_candles_before_idea_are_ignored(self):
        trade = advance(LONG, [Bar(T0 - STEP, 101, 102, 100, 101)])
        self.assertEqual(trade.status, "WAITING")

    def test_candle_started_before_idea_is_ignored(self):
        created = replace(LONG, created_at=T0 + timedelta(minutes=2))
        self.assertEqual(advance(created, [bar(0, 106, 101)]).status, "WAITING")

    def test_last_seen_candle_is_not_replayed(self):
        trade = advance(LONG, [bar(0, 106, 104)])
        again = advance(trade, [bar(0, 106, 101)])
        self.assertEqual(again.status, "WAITING")


class UntrackableTest(unittest.TestCase):
    def test_trade_without_stop_stays_off(self):
        trade = replace(LONG, stop=None)
        self.assertEqual(advance(trade, [bar(0, 106, 100)]).status, "NOT_TRACKED")

    def test_wrong_level_order_stays_off(self):
        trade = replace(LONG, stop=103)
        self.assertEqual(advance(trade, [bar(0, 106, 100)]).status, "NOT_TRACKED")


class RewardRiskTest(unittest.TestCase):
    def test_long(self):
        self.assertAlmostEqual(reward_risk(LONG), 11 / 6)

    def test_short(self):
        self.assertAlmostEqual(reward_risk(SHORT), 2)

    def test_missing_target(self):
        self.assertIsNone(reward_risk(replace(LONG, target=None)))


if __name__ == "__main__":
    unittest.main()
