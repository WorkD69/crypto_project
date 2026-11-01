import unittest

from signal_bot.fixes import REJECT, parse_fix

SYMBOLS = {"BTCUSDT", "ETHUSDT"}


class ParseFixTest(unittest.TestCase):
    def test_no_zone_at_all(self):
        for text in ("нет", "Нет зоны", "зоны нет"):
            self.assertIs(parse_fix(text, SYMBOLS), REJECT, text)

    def test_full_correction(self):
        self.assertEqual(parse_fix("шорт 82000-84000 стоп 85500 цели 78000 76000", SYMBOLS),
                         {"side": "SHORT", "zone_low": 82000.0, "zone_high": 84000.0, "stop": 85500.0,
                          "targets": [78000.0, 76000.0]})

    def test_single_field(self):
        self.assertEqual(parse_fix("стоп 85500", SYMBOLS), {"stop": 85500.0})

    def test_stop_can_be_removed(self):
        self.assertEqual(parse_fix("стоп нет", SYMBOLS), {"stop": None})

    def test_only_the_side(self):
        self.assertEqual(parse_fix("лонг", SYMBOLS), {"side": "LONG"})

    def test_other_coin(self):
        self.assertEqual(parse_fix("ETH лонг 2380-2460", SYMBOLS),
                         {"symbol": "ETHUSDT", "side": "LONG", "zone_low": 2380.0, "zone_high": 2460.0})

    def test_thousands_suffix_covers_the_whole_range(self):
        result = parse_fix("82-84k", SYMBOLS)
        self.assertEqual((result["zone_low"], result["zone_high"]), (82000.0, 84000.0))

    def test_cyrillic_suffix_on_both_numbers(self):
        result = parse_fix("82к-84.5к", SYMBOLS)
        self.assertEqual((result["zone_low"], result["zone_high"]), (82000.0, 84500.0))

    def test_decimal_comma(self):
        result = parse_fix("0,42–0,44", SYMBOLS)
        self.assertEqual((result["zone_low"], result["zone_high"]), (0.42, 0.44))

    def test_range_is_sorted(self):
        self.assertEqual(parse_fix("84000-82000", SYMBOLS)["zone_low"], 82000.0)

    def test_single_level(self):
        result = parse_fix("уровень 140", SYMBOLS)
        self.assertEqual((result["zone_low"], result["zone_high"]), (140.0, 140.0))

    def test_stop_and_targets_do_not_become_a_range(self):
        self.assertNotIn("zone_low", parse_fix("стоп 85500 цель 78000-76000", SYMBOLS))

    def test_target_word_forms(self):
        for word in ("цель", "цели", "тейк", "тейки"):
            self.assertEqual(parse_fix(f"{word} 78000", SYMBOLS), {"targets": [78000.0]}, word)

    def test_words_that_say_nothing_are_refused(self):
        self.assertIsNone(parse_fix("что-то не то", SYMBOLS))

    def test_unknown_coin_is_refused(self):
        self.assertIsNone(parse_fix("NOSUCHCOIN лонг", SYMBOLS))


if __name__ == "__main__":
    unittest.main()
