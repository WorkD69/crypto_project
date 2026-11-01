import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from telegram import Chat, Message, MessageOriginChannel, MessageOriginHiddenUser, MessageOriginUser, User
from telethon.tl.types import MessageMediaPhoto, MessageMediaWebPage

from signal_bot.app import origin_of
from signal_bot.reader import Source, catch_up, has_photo, load_sources, origin_key, post_from

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
PUBLIC = Source(id=1000000001, name="Public Author", username="example_public")
PRIVATE = Source(id=1000000002, name="Private Author", username=None)


def message(message_id=9, text="x", grouped_id=None, media=None):
    return SimpleNamespace(id=message_id, message=text, date=T0, grouped_id=grouped_id, media=media)


class SourceTest(unittest.TestCase):
    def test_load_sources(self):
        sources = load_sources('[{"id": 5, "name": "A", "username": null}]')
        self.assertEqual(sources, {5: Source(5, "A", None)})

    def test_public_post_link(self):
        post = post_from(PUBLIC, message(77, "BTC лонг"))
        self.assertEqual(post.url, "https://t.me/example_public/77")
        self.assertEqual(post.author, "Public Author")
        self.assertEqual(post.text, "BTC лонг")

    def test_private_post_link(self):
        self.assertEqual(post_from(PRIVATE, message()).url, "https://t.me/c/1000000002/9")

    def test_media_without_text_gives_empty_text(self):
        self.assertEqual(post_from(PUBLIC, message(text=None)).text, "")

    def test_post_keeps_its_time(self):
        self.assertEqual(post_from(PUBLIC, message()).posted_at, T0)

    def test_single_post_is_no_album(self):
        self.assertIsNone(post_from(PUBLIC, message()).album)

    def test_album_parts_share_a_key_within_the_channel(self):
        first, second = post_from(PUBLIC, message(9, grouped_id=5)), post_from(PUBLIC, message(10, grouped_id=5))
        self.assertEqual(first.album, second.album)
        self.assertNotEqual(post_from(PRIVATE, message(9, grouped_id=5)).album, first.album)

    def test_each_part_has_its_own_key(self):
        self.assertEqual(post_from(PUBLIC, message(9)).part_key, origin_key(PUBLIC.id, 9))


class PhotoTest(unittest.TestCase):
    def test_photo(self):
        self.assertTrue(has_photo(message(media=MessageMediaPhoto())))

    def test_link_preview_is_not_a_photo(self):
        self.assertFalse(has_photo(message(media=MessageMediaWebPage(webpage=None))))

    def test_text_post(self):
        self.assertFalse(has_photo(message()))


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
