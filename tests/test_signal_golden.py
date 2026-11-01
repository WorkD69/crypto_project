import unittest
from dataclasses import dataclass
from datetime import datetime, timezone

from pathlib import Path

from signal_bot.golden import group_album, score, set_files, zone_matches


def label(symbol="BTCUSDT", side="SHORT", low=82000, high=84000):
    return {"symbol": symbol, "side": side, "low": low, "high": high}


def predicted(symbol="BTCUSDT", side="SHORT", low=82000.0, high=84000.0):
    return {"symbol": symbol, "side": side, "zone_low": low, "zone_high": high}


class MatchTest(unittest.TestCase):
    def test_exact_bounds(self):
        self.assertTrue(zone_matches(label(), predicted()))

    def test_bounds_within_tolerance(self):
        self.assertTrue(zone_matches(label(), predicted(low=82000 * 1.015, high=84000 * 0.985)))

    def test_one_bound_outside_tolerance(self):
        self.assertFalse(zone_matches(label(), predicted(high=84000 * 1.016)))

    def test_missing_bounds_fail(self):
        self.assertFalse(zone_matches(label(), predicted(low=None, high=None)))


class ScoreTest(unittest.TestCase):
    def report(self, *cases):
        return score([{"labels": labels, "predicted": found} for labels, found in cases])

    def test_all_correct_passes(self):
        report = self.report(([label()], [predicted()]))
        self.assertEqual((report["coin_side"], report["bounds"]), (1.0, 1.0))
        self.assertTrue(report["passed"])

    def test_wrong_side_fails_both_measures(self):
        report = self.report(([label()], [predicted(side="LONG")]))
        self.assertEqual((report["coin_side"], report["bounds"]), (0.0, 0.0))

    def test_right_coin_with_wrong_bounds_counts_only_for_coin(self):
        report = self.report(([label()], [predicted(low=70000, high=71000)]))
        self.assertEqual((report["coin_side"], report["bounds"]), (1.0, 0.0))

    def test_missing_zone_counts_against_both(self):
        report = self.report(([label(), label(symbol="ETHUSDT", low=2380, high=2460)], [predicted()]))
        self.assertEqual((report["coin_side"], report["bounds"]), (0.5, 0.5))

    def test_each_prediction_is_used_once(self):
        report = self.report(([label(), label()], [predicted()]))
        self.assertEqual(report["coin_side"], 0.5)

    def test_better_of_two_same_coin_zones_is_chosen(self):
        report = self.report(([label()], [predicted(low=1, high=2), predicted()]))
        self.assertEqual(report["bounds"], 1.0)

    def test_two_zones_of_one_coin_pair_up_correctly(self):
        labels = [label(low=80000, high=81000), label(low=90000, high=91000)]
        found = [predicted(low=90000, high=91000), predicted(low=80000, high=81000)]
        self.assertEqual(self.report((labels, found))["bounds"], 1.0)

    def test_extra_zone_is_counted_but_not_a_miss(self):
        report = self.report(([label()], [predicted(), predicted(symbol="ETHUSDT", low=2380, high=2460)]))
        self.assertEqual(report["extra"], 1)
        self.assertEqual(report["coin_side"], 1.0)

    def test_empty_post_with_zones_is_a_false_alarm(self):
        report = self.report(([label()], [predicted()]), ([], [predicted()]))
        self.assertEqual(report["false_alarm_posts"], 1)

    def test_report_of_a_past_trade_is_not_a_new_zone(self):
        report = self.report(([], [{**predicted(), "action": "result"}]))
        self.assertEqual(report["false_alarm_posts"], 0)
        self.assertEqual(report["extra"], 0)

    def test_exchange_prefix_is_ignored(self):
        report = self.report(([label(symbol="TOTAL2")], [predicted(symbol="CRYPTOCAP:TOTAL2")]))
        self.assertEqual(report["coin_side"], 1.0)

    def test_thresholds_are_ninety_and_eighty(self):
        good = ([label()], [predicted()])
        coin_only = ([label()], [predicted(low=1, high=2)])
        miss = ([label()], [])
        self.assertTrue(self.report(*[good] * 8, coin_only, coin_only)["passed"])
        self.assertFalse(self.report(*[good] * 7, coin_only, coin_only, coin_only)["passed"])
        self.assertTrue(self.report(*[good] * 8, coin_only, miss)["passed"])
        self.assertFalse(self.report(*[good] * 8, miss, miss)["passed"])

    def test_no_labels_never_pass(self):
        self.assertFalse(self.report(([], []))["passed"])


class SetFilesTest(unittest.TestCase):
    def test_default_set(self):
        self.assertEqual(set_files(Path("g"), ""), (Path("g/labels.json"), Path("g/results.json")))

    def test_named_set_keeps_its_own_files(self):
        self.assertEqual(set_files(Path("g"), "fresh"), (Path("g/labels-fresh.json"), Path("g/results-fresh.json")))


@dataclass
class Message:
    id: int
    grouped_id: int | None = None
    message: str = ""
    photo: object = None
    date: datetime = datetime(2026, 11, 1, tzinfo=timezone.utc)


class AlbumTest(unittest.TestCase):
    def test_album_parts_are_joined_with_their_caption(self):
        posts = group_album([Message(3, 7, "", True), Message(2, 7, "подпись", True), Message(1, None, "текст", True)])
        self.assertEqual([[part.id for part in post] for post in posts], [[2, 3], [1]])

    def test_posts_without_photos_are_left_out(self):
        self.assertEqual(group_album([Message(1, None, "текст")]), [])


if __name__ == "__main__":
    unittest.main()
