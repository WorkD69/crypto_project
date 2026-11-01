import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from telegram import Chat, Message, MessageOriginChannel, MessageOriginHiddenUser, MessageOriginUser, User

from signal_bot.app import origin_of
from signal_bot.reader import Source, catch_up, load_sources, origin_key, post_from

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
PUBLIC = Source(id=1000000001, name="Public Author", username="example_public")
PRIVATE = Source(id=1000000002, name="Private Author", username=None)


class SourceTest(unittest.TestCase):
    def test_load_sources(self):
        sources = load_sources('[{"id": 5, "name": "A", "username": null}]')
        self.assertEqual(sources, {5: Source(5, "A", None)})

    def test_public_post_link(self):
        post = post_from(PUBLIC, SimpleNamespace(id=77, message="BTC лонг"))
        self.assertEqual(post.url, "https://t.me/example_public/77")
        self.assertEqual(post.author, "Public Author")
        self.assertEqual(post.text, "BTC лонг")

    def test_private_post_link(self):
        post = post_from(PRIVATE, SimpleNamespace(id=9, message="x"))
        self.assertEqual(post.url, "https://t.me/c/1000000002/9")

    def test_media_without_text_gives_empty_text(self):
        self.assertEqual(post_from(PUBLIC, SimpleNamespace(id=9, message=None)).text, "")


class CatchUpTest(unittest.TestCase):
    class Client:
        def __init__(self, ids):
            self.ids, self.calls = ids, []

        def iter_messages(self, entity, min_id=0, limit=None):
            self.calls.append((entity, min_id, limit))

            async def generate():
                for message_id in sorted((i for i in self.ids if i > min_id), reverse=True)[:limit]:
                    yield SimpleNamespace(id=message_id, message=f"m{message_id}")
            return generate()

    def run_catch_up(self, ids, cursor):
        seen = []

        async def handle(source, message):
            seen.append(message.id)
        latest = asyncio.run(catch_up(self.Client(ids), PUBLIC, cursor, handle))
        return seen, latest

    def test_first_run_only_remembers_latest_post(self):
        self.assertEqual(self.run_catch_up([1, 2, 3], None), ([], 3))

    def test_missed_posts_are_handled_oldest_first(self):
        self.assertEqual(self.run_catch_up([1, 2, 3, 4], 2), ([3, 4], 4))

    def test_nothing_new_keeps_cursor(self):
        self.assertEqual(self.run_catch_up([1, 2], 2), ([], 2))

    def test_empty_channel(self):
        self.assertEqual(self.run_catch_up([], None), ([], None))


class OriginTest(unittest.TestCase):
    def message(self, origin):
        return Message(1, T0, Chat(1, "private"), forward_origin=origin, text="x")

    def test_channel_forward_shares_key_with_reader(self):
        chat = Chat(-1001000000001, "channel", title="PUBLIC", username="example_public")
        author, key, url = origin_of(self.message(MessageOriginChannel(T0, chat, 77)))
        self.assertEqual(author, "PUBLIC")
        self.assertEqual(key, origin_key(1000000001, 77))
        self.assertEqual(url, "https://t.me/example_public/77")

    def test_private_channel_link(self):
        chat = Chat(-1001000000002, "channel", title="PRIVATE")
        self.assertEqual(origin_of(self.message(MessageOriginChannel(T0, chat, 9)))[2],
                         "https://t.me/c/1000000002/9")

    def test_user_forward_has_no_key(self):
        origin = MessageOriginUser(T0, User(5, "Андрей", False))
        self.assertEqual(origin_of(self.message(origin)), ("Андрей", None, None))

    def test_hidden_user_forward(self):
        self.assertEqual(origin_of(self.message(MessageOriginHiddenUser(T0, "Скрыт")))[0], "Скрыт")

    def test_typed_message(self):
        self.assertEqual(origin_of(self.message(None)), ("Ты", None, None))


if __name__ == "__main__":
    unittest.main()
