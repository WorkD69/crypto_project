import unittest

from signal_bot.extract import build_prompt
from signal_bot.hermes import BadAnswer
from signal_bot.lessons import image_url, page_content, page_slug, parse_digest, parse_rules, split_paragraphs, \
    verified_rules

PAGE = {"url": "https://telegra.ph/Primer-01-01", "title": "Пример", "content": [
    {"tag": "p", "children": ["Первый ", {"tag": "b", "children": ["абзац"]}, " урока"]},
    {"tag": "figure", "children": [{"tag": "img", "attrs": {"src": "/file/abc.jpg"}}]},
    {"tag": "p", "children": ["  "]},
    {"tag": "p", "children": ["Зона лежит между 61.8 и 78.6 по фибоначчи"]},
    {"tag": "figure", "children": [{"tag": "img", "attrs": {"src": "https://evil.example/x.jpg"}}]}]}


class AddressTest(unittest.TestCase):
    def test_telegraph_page_is_accepted(self):
        self.assertEqual(page_slug("https://telegra.ph/Primer-01-01"), "Primer-01-01")

    def test_other_addresses_are_refused(self):
        for url in ("http://telegra.ph/Primer", "https://telegra.ph.evil.com/Primer", "https://telegra.ph/a/b",
                    "https://example.com/Primer", "https://telegra.ph/Primer?x=1"):
            self.assertIsNone(page_slug(url), url)

    def test_telegraph_image_is_accepted(self):
        self.assertEqual(image_url("/file/abc.jpg"), "https://telegra.ph/file/abc.jpg")

    def test_foreign_image_is_refused(self):
        self.assertIsNone(image_url("https://evil.example/x.jpg"))
        self.assertIsNone(image_url("/file/../secret"))


class ContentTest(unittest.TestCase):
    def test_paragraphs_are_flattened_with_ids(self):
        content = page_content(PAGE)
        self.assertEqual(content["paragraphs"][0], {"id": "p0", "text": "Первый абзац урока"})
        self.assertEqual(content["paragraphs"][1]["id"], "p3")

    def test_only_telegraph_images_are_kept(self):
        self.assertEqual(page_content(PAGE)["images"], ["https://telegra.ph/file/abc.jpg"])

    def test_long_lesson_is_split_on_paragraphs(self):
        paragraphs = [{"id": f"p{i}", "text": "я" * 400} for i in range(5)]
        parts = split_paragraphs(paragraphs, 900)
        self.assertEqual([len(part) for part in parts], [2, 2, 1])


class RulesTest(unittest.TestCase):
    paragraphs = page_content(PAGE)["paragraphs"]

    def rule(self, **changes):
        return {"rule": "Вход от зоны фибы", "quote": "между 61.8 и 78.6", "paragraph_id": "p3", **changes}

    def test_rule_with_exact_quote_is_kept(self):
        self.assertEqual(len(verified_rules([self.rule()], self.paragraphs)), 1)

    def test_invented_quote_is_dropped(self):
        self.assertEqual(verified_rules([self.rule(quote="этого в уроке нет вообще")], self.paragraphs), [])

    def test_quote_from_another_paragraph_is_dropped(self):
        self.assertEqual(verified_rules([self.rule(paragraph_id="p0")], self.paragraphs), [])

    def test_whole_paragraph_can_be_quoted(self):
        text = "я " * 300
        kept = verified_rules([self.rule(quote=text.strip(), paragraph_id="p9")], [{"id": "p9", "text": text}])
        self.assertEqual(len(kept), 1)

    def test_too_short_quote_is_dropped(self):
        self.assertEqual(verified_rules([self.rule(quote="61")], self.paragraphs), [])

    def test_answer_without_valid_rules_is_refused(self):
        with self.assertRaises(BadAnswer):
            parse_rules({"rules": [self.rule(quote="выдумка автора модели")]}, self.paragraphs)

    def test_answer_of_wrong_shape_is_refused(self):
        with self.assertRaises(BadAnswer):
            parse_rules({"rules": "текст"}, self.paragraphs)


class DigestTest(unittest.TestCase):
    source = "Зона лежит между 61.8 и 78.6 по фибоначчи"

    def test_numbers_from_rules_pass(self):
        self.assertEqual(parse_digest({"digest": "Откат в зону 61.8–78.6."}, self.source), "Откат в зону 61.8–78.6.")

    def test_invented_number_is_refused(self):
        with self.assertRaises(BadAnswer):
            parse_digest({"digest": "Откат в зону 50–78.6."}, self.source)

    def test_oversized_digest_is_refused(self):
        with self.assertRaises(BadAnswer):
            parse_digest({"digest": "я" * 9000}, self.source)


class PromptTest(unittest.TestCase):
    def test_knowledge_goes_before_the_post(self):
        prompt = build_prompt("текст поста", "памятка по уроку")
        self.assertLess(prompt.index("памятка по уроку"), prompt.index("<post>"))

    def test_no_knowledge_block_without_knowledge(self):
        self.assertNotIn("<knowledge>", build_prompt("текст поста"))


if __name__ == "__main__":
    unittest.main()
