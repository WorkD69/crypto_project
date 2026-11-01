import unittest
from dataclasses import replace
from datetime import datetime, timezone

from signal_bot.cards import chunks, render_digest, render_event, render_open, render_stats, \
    render_unparsed, render_zone, render_zone_delete, render_zone_edit, render_zone_event
from signal_bot.tracker import Trade

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def idea(**overrides):
    base = dict(id=1, symbol="BTCUSDT", side="LONG", entry_low=100.0, entry_high=102.0, stop=95.0,
                targets=[112.0, 120.0], entry_assumed=0, price=103.0, warnings=[], author="Крипто Илья",
                url="https://t.me/x/1", text="Беру лонг от уровня", status="WAITING", decision=None,
                result_r=None)
    return {**base, **overrides}


TRADE = Trade(side="LONG", entry_low=100, entry_high=102, stop=95, target=112, created_at=T0)


def zone(**overrides):
    base = dict(id=7, symbol="BTCUSDT", side="SHORT", zone_low=82000.0, zone_high=84000.0, stop=None,
                targets=[78000.0, 76000.0], timeframe="4H", trigger="zone", confidence=0.8, evidence="красная рамка",
                price=80000.0, flags=[], author="Мысли Вайкоффа", url="https://t.me/x/1", status="WATCHING",
                version=1, decision=None)
    return {**base, **overrides}


class ZoneCardTest(unittest.TestCase):
    def test_card_shows_the_whole_zone(self):
        card = render_zone(zone(stop=85500.0))
        for piece in ("ШОРТ", "BTCUSDT", "4H", "Мысли Вайкоффа", "82000–84000", "85500", "78000, 76000", "80%",
                      "красная рамка", 'href="https://t.me/x/1"'):
            self.assertIn(piece, card)

    def test_missing_stop_is_said_aloud(self):
        self.assertIn("стоп не указан", render_zone(zone()))

    def test_distance_up_to_the_zone(self):
        self.assertIn("+2.50%", render_zone(zone()))

    def test_distance_down_to_the_zone(self):
        self.assertIn("-2.50%", render_zone(zone(side="LONG", zone_low=76000.0, zone_high=78000.0, targets=[])))

    def test_price_inside_the_zone(self):
        self.assertIn("цена в зоне", render_zone(zone(zone_low=79000.0, zone_high=81000.0)))

    def test_unknown_price_does_not_crash(self):
        self.assertNotIn("%, ", render_zone(zone(price=None)).split("Уверенность")[0])

    def test_single_level_is_one_number(self):
        self.assertNotIn("–", render_zone(zone(zone_low=82000.0, zone_high=82000.0)).split("Стоп")[0])

    def test_break_of_a_short_level(self):
        card = render_zone(zone(trigger="break", zone_low=140.0, zone_high=140.0, price=150.0, targets=[100.0]))
        self.assertIn("при потере уровня 140", card)

    def test_break_of_a_long_level(self):
        card = render_zone(zone(side="LONG", trigger="break", zone_low=160.0, zone_high=160.0, price=150.0))
        self.assertIn("при пробое уровня 160", card)

    def test_plain_zone_says_nothing_about_a_break(self):
        self.assertNotIn("пробо", render_zone(zone()))

    def test_chart_only_zone_asks_for_eyes(self):
        card = render_zone(zone(flags=["CHART_ONLY", "CHECK_BY_EYE"]))
        self.assertIn("Проверь глазами", card)
        self.assertIn("только с графика", card)

    def test_text_zone_has_no_such_note(self):
        self.assertNotIn("Проверь глазами", render_zone(zone()))

    def test_every_flag_is_readable(self):
        for flag in ("NOT_IN_POST", "CHART_PRICE_DRIFT", "LEVEL_FAR_FROM_PRICE", "UNKNOWN_SYMBOL", "PRICE_UNAVAILABLE",
                     "SCALE_FIXED", "OWNER_FIX"):
            self.assertNotIn(flag, render_zone(zone(flags=[flag])))
            self.assertIn("⚠️", render_zone(zone(flags=[flag])))

    def test_lost_bounds_are_said_aloud(self):
        self.assertIn("границы не прочитаны", render_zone(zone(zone_low=None, zone_high=None)))

    def test_author_words_cannot_inject_html(self):
        card = render_zone(zone(evidence="<b>жирно</b>", author="A<B"))
        self.assertNotIn("<b>жирно", card)
        self.assertIn("A&lt;B", card)

    def test_no_link_for_a_hidden_source(self):
        self.assertNotIn("href", render_zone(zone(url=None)))


class ZoneEventTest(unittest.TestCase):
    def test_cancel(self):
        text = render_zone_event(zone(), {"kind": "cancel", "before": None}, "https://t.me/x/2")
        self.assertIn("отменил", text)
        self.assertIn("82000–84000", text)
        self.assertIn('href="https://t.me/x/2"', text)

    def test_result(self):
        self.assertIn("итог", render_zone_event(zone(), {"kind": "result", "before": None}, None))

    def test_update_shows_old_and_new_levels(self):
        before = {"zone_low": 80000.0, "zone_high": 81000.0, "stop": None, "targets": [70000.0]}
        text = render_zone_event(zone(stop=85500.0), {"kind": "update", "before": before}, None)
        for piece in ("обновил", "80000–81000", "82000–84000", "85500"):
            self.assertIn(piece, text)


class UnparsedTest(unittest.TestCase):
    def post(self, **overrides):
        return {**dict(author="Автор", text="Пост про рынок", edited_text=None, url="https://t.me/x/1"), **overrides}

    def test_notice_says_nothing_was_lost(self):
        text = render_unparsed(self.post(), 0)
        for piece in ("без разбора", "Автор", "Пост про рынок", 'href="https://t.me/x/1"'):
            self.assertIn(piece, text)

    def test_album_without_caption_is_described(self):
        self.assertIn("картинок: 3", render_unparsed(self.post(text=""), 3))

    def test_author_text_is_escaped(self):
        self.assertIn("&lt;b&gt;", render_unparsed(self.post(text="<b>"), 0))


class ZoneNewsTest(unittest.TestCase):
    def test_edit_names_the_coins_of_the_post(self):
        text = render_zone_edit([zone(), zone(symbol="ETHUSDT")], "новый текст")
        self.assertIn("BTCUSDT", text)
        self.assertIn("ETHUSDT", text)
        self.assertIn("новый текст", text)

    def test_delete_says_the_zone_stays(self):
        self.assertIn("удалил", render_zone_delete([zone()]))

    def test_digest_lists_zones_and_the_queue(self):
        text = render_digest([], [], [zone()], 2)
        self.assertIn("BTCUSDT", text)
        self.assertIn("Без разбора: 2", text)

    def test_open_lists_zones(self):
        text = render_open([], [zone()])
        self.assertIn("BTCUSDT", text)
        self.assertIn("82000–84000", text)


class EventTest(unittest.TestCase):
    def test_entered(self):
        text = render_event(idea(), replace(TRADE, status="ENTERED", fill_price=101.0))
        self.assertIn("вход сработал", text)
        self.assertIn("101", text)

    def test_take_profit_shows_r(self):
        text = render_event(idea(), replace(TRADE, status="TP", result_r=1.81))
        self.assertIn("+1.81R", text)

    def test_stop_shows_negative_r(self):
        self.assertIn("-1.02R", render_event(idea(), replace(TRADE, status="SL", result_r=-1.02)))

    def test_ambiguous_stop_is_explained(self):
        text = render_event(idea(), replace(TRADE, status="SL", result_r=-1.02, ambiguous=True))
        self.assertIn("одной свече", text)

    def test_every_finish_has_text(self):
        for status in ("MISSED", "EXPIRED", "TIMEOUT"):
            self.assertIn("BTCUSDT", render_event(idea(), replace(TRADE, status=status, result_r=0.1)))


class SummaryTest(unittest.TestCase):
    def test_digest_groups_opinions_by_author(self):
        opinions = [{"author": "Автор А", "text": "Рынок слабый"}, {"author": "Автор Б", "text": "Жду откат"}]
        text = render_digest(opinions, [idea()])
        self.assertIn("Автор А", text)
        self.assertIn("Жду откат", text)
        self.assertIn("BTCUSDT", text)

    def test_empty_digest(self):
        self.assertIn("ничего", render_digest([], []).lower())

    def test_stats_show_author_results(self):
        rows = [{"author": "Автор А", "ideas": 4, "closed": 2, "wins": 1, "total_r": 1.0, "taken_r": 2.0, "deleted": 1}]
        text = render_stats(rows)
        for piece in ("Автор А", "50%", "+1.00R", "+2.00R", "удалено 1"):
            self.assertIn(piece, text)

    def test_stats_without_closed_ideas(self):
        rows = [{"author": "Автор А", "ideas": 1, "closed": 0, "wins": 0, "total_r": 0, "taken_r": 0, "deleted": 0}]
        self.assertIn("пока нет", render_stats(rows))

    def test_empty_stats(self):
        self.assertIn("Пока нет", render_stats([]))

    def test_open_list(self):
        self.assertIn("BTCUSDT", render_open([idea(status="ENTERED")]))
        self.assertIn("Открытых идей нет", render_open([]))


class ChunksTest(unittest.TestCase):
    def test_short_text_stays_whole(self):
        self.assertEqual(chunks("abc"), ["abc"])

    def test_long_text_splits_on_lines_under_limit(self):
        parts = chunks("\n".join(["строка" * 50] * 100))
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 4000 for part in parts))

    def test_single_giant_line_is_cut(self):
        self.assertTrue(all(len(part) <= 4000 for part in chunks("я" * 9000)))


if __name__ == "__main__":
    unittest.main()
