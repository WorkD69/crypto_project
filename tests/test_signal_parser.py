import unittest

from signal_bot.parser import apply_price, parse_post, resolve_number

SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "1000PEPEUSDT", "ALGOUSDT"}


class SymbolTest(unittest.TestCase):
    def test_hashtag_symbol(self):
        self.assertEqual(parse_post("#SOL вход 150 стоп 140", SYMBOLS).symbol, "SOLUSDT")

    def test_pair_with_slash(self):
        self.assertEqual(parse_post("ALGO/USDT вход 0.2", SYMBOLS).symbol, "ALGOUSDT")

    def test_full_ticker_with_digits(self):
        self.assertEqual(parse_post("1000PEPEUSDT лонг, стоп 0.01", SYMBOLS).symbol, "1000PEPEUSDT")

    def test_russian_alias(self):
        self.assertEqual(parse_post("Биткоин: вход 80000, стоп 78000", SYMBOLS).symbol, "BTCUSDT")

    def test_market_terms_are_not_coins(self):
        self.assertIsNone(parse_post("TP и SL по FVG, RR хороший", SYMBOLS).symbol)

    def test_unknown_ticker_is_ignored(self):
        self.assertIsNone(parse_post("#NOSUCHCOIN вход 5", SYMBOLS).symbol)


class SideTest(unittest.TestCase):
    def test_long_word(self):
        self.assertEqual(parse_post("BTC лонг", SYMBOLS).side, "LONG")

    def test_short_word_with_ending(self):
        self.assertEqual(parse_post("ETH шорта не будет бояться", SYMBOLS).side, "SHORT")

    def test_both_directions_stay_undefined(self):
        self.assertIsNone(parse_post("BTC: лонг или шорт, пока неясно", SYMBOLS).side)

    def test_side_inferred_from_levels(self):
        parsed = apply_price(parse_post("BTC вход 80000 стоп 78000 тейк 84000", SYMBOLS), 80100)
        self.assertEqual(parsed.side, "LONG")
        self.assertIn("SIDE_FROM_LEVELS", parsed.warnings)


class LevelsTest(unittest.TestCase):
    def test_levels_on_one_line(self):
        parsed = apply_price(parse_post("BTC лонг вход 80000 стоп 78000 тейк 84000", SYMBOLS), 80100)
        self.assertEqual((parsed.entry_low, parsed.entry_high), (80000, 80000))
        self.assertEqual(parsed.stop, 78000)
        self.assertEqual(parsed.targets, [84000])

    def test_entry_range_is_sorted(self):
        parsed = apply_price(parse_post("SOL лонг\nТВХ: 152-148\nSL 140", SYMBOLS), 150)
        self.assertEqual((parsed.entry_low, parsed.entry_high), (148, 152))

    def test_numbered_targets_are_collected_in_order(self):
        text = "ETH шорт\nВход 3000\nСтоп 3100\nTP1: 2900\nTP2: 2800"
        self.assertEqual(apply_price(parse_post(text, SYMBOLS), 3000).targets, [2900, 2800])

    def test_list_markers_are_not_prices(self):
        parsed = apply_price(parse_post("ETH лонг вход 3000 стоп 2900 цели: 1) 3100 2) 3200", SYMBOLS), 3000)
        self.assertEqual(parsed.targets, [3100, 3200])

    def test_percent_stop_is_not_a_price(self):
        parsed = apply_price(parse_post("BTC лонг вход 80000 стоп 3%", SYMBOLS), 80000)
        self.assertIsNone(parsed.stop)

    def test_level_far_from_market_is_dropped_with_warning(self):
        parsed = apply_price(parse_post("BTC лонг вход 80000 стоп 7", SYMBOLS), 80000)
        self.assertIsNone(parsed.stop)
        self.assertIn("LEVEL_FAR_FROM_PRICE", parsed.warnings)

    def test_unreadable_number_is_skipped(self):
        parsed = apply_price(parse_post("BTC лонг вход 80000 стоп 1.2.3", SYMBOLS), 80000)
        self.assertIsNone(parsed.stop)
        self.assertEqual(parsed.entry_low, 80000)

    def test_works_without_market_price(self):
        parsed = apply_price(parse_post("BTC лонг вход 80000 стоп 78000", SYMBOLS), None)
        self.assertEqual(parsed.stop, 78000)
        self.assertIn("PRICE_UNAVAILABLE", parsed.warnings)


class NumberTest(unittest.TestCase):
    def test_dot_as_thousands_uses_market_price(self):
        self.assertEqual(resolve_number("2.350", 2340), 2350)

    def test_dot_as_decimal_uses_market_price(self):
        self.assertEqual(resolve_number("2.350", 2.3), 2.35)

    def test_space_thousands(self):
        self.assertEqual(resolve_number("84 188", 84000), 84188)

    def test_comma_thousands_with_decimal(self):
        self.assertEqual(resolve_number("84,188.1", 84000), 84188.1)

    def test_comma_decimal(self):
        self.assertEqual(resolve_number("0,245", 0.25), 0.245)


class ClassificationTest(unittest.TestCase):
    def test_coin_with_levels_is_idea(self):
        self.assertTrue(parse_post("BTC лонг стоп 78000", SYMBOLS).is_idea)

    def test_opinion_without_levels_is_not_idea(self):
        self.assertFalse(parse_post("BTC выглядит сильным, жду роста", SYMBOLS).is_idea)

    def test_levels_without_coin_are_not_idea(self):
        self.assertFalse(parse_post("вход 100 стоп 90", SYMBOLS).is_idea)


if __name__ == "__main__":
    unittest.main()
