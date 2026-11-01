import unittest
from dataclasses import replace
from datetime import datetime, timezone

from signal_bot.cards import chunks, render_card, render_digest, render_event, render_open, render_stats
from signal_bot.tracker import Trade

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def idea(**overrides):
    base = dict(id=1, symbol="BTCUSDT", side="LONG", entry_low=100.0, entry_high=102.0, stop=95.0,
                targets=[112.0, 120.0], entry_assumed=0, price=103.0, warnings=[], author="Крипто Илья",
                url="https://t.me/x/1", text="Беру лонг от уровня", status="WAITING", decision=None,
                result_r=None)
    return {**base, **overrides}


TRADE = Trade(side="LONG", entry_low=100, entry_high=102, stop=95, target=112, created_at=T0)


class CardTest(unittest.TestCase):
    def test_card_shows_all_levels(self):
        card = render_card(idea())
        for piece in ("ЛОНГ", "BTCUSDT", "Крипто Илья", "100–102", "95", "112", "120", "103", "1.83"):
            self.assertIn(piece, card)

    def test_card_shows_short(self):
        self.assertIn("ШОРТ", render_card(idea(side="SHORT")))

    def test_assumed_entry_is_labelled(self):
        card = render_card(idea(entry_low=103.0, entry_high=103.0, entry_assumed=1))
        self.assertIn("по рынку", card)

    def test_warnings_are_readable(self):
        card = render_card(idea(warnings=["LEVEL_FAR_FROM_PRICE", "SIDE_FROM_LEVELS"]))
        self.assertIn("далеко от цены", card)
        self.assertIn("Направление определено по уровням", card)

    def test_untracked_idea_says_so(self):
        self.assertIn("Не отслеживается", render_card(idea(stop=None, status="NOT_TRACKED")))

    def test_author_text_cannot_inject_html(self):
        card = render_card(idea(text="<b>ссылка</b> & всё", author="A<B"))
        self.assertNotIn("<b>ссылка", card)
        self.assertIn("&lt;b&gt;ссылка", card)
        self.assertIn("A&lt;B", card)

    def test_long_post_is_shortened(self):
        self.assertLess(len(render_card(idea(text="а" * 5000))), 1500)

    def test_link_to_original(self):
        self.assertIn('href="https://t.me/x/1"', render_card(idea()))

    def test_missing_values_do_not_crash(self):
        card = render_card(idea(side=None, entry_low=None, entry_high=None, stop=None, targets=[], price=None,
                                url=None, status="NOT_TRACKED"))
        self.assertIn("не указан", card)


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
