import asyncio
import inspect
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from telegram import Bot

from signal_bot.app import SignalBot, keyboard
from signal_bot.reader import Source, origin_key
from signal_bot.storage import Storage

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
OWNER = 7
SOURCE = Source(1000000001, "Public Author", "example_public")
EVENT_CHAT = -1001000000001


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        inspect.signature(Bot.send_message).bind(None, chat_id, text, **kwargs)
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, **kwargs))
        return SimpleNamespace(message_id=100 + len(self.sent))


class FakeMarket:
    async def symbols(self):
        return {"BTCUSDT"}

    async def price(self, symbol):
        return 80000.0


class FakeMessage:
    def __init__(self, text="", media_group_id=None):
        self.text, self.caption, self.media_group_id, self.forward_origin = text, None, media_group_id, None
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)


class FakeQuery:
    def __init__(self, data):
        self.data, self.answers, self.markups = data, [], []

    async def answer(self, text=None):
        self.answers.append(text)

    async def edit_message_reply_markup(self, markup):
        self.markups.append(markup)


def update(user_id, message=None, query=None):
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_message=message, callback_query=query)


class AppCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = Storage(Path(self.directory.name) / "bot.sqlite3")
        self.signal = SignalBot(self.storage, {SOURCE.id: SOURCE}, FakeMarket())
        self.signal.bot = FakeBot()

    def own(self):
        self.storage.claim_owner(OWNER)

    def channel_post(self, message_id=5, text="BTC лонг вход 80000 стоп 78000 тейк 84000"):
        event = SimpleNamespace(chat_id=EVENT_CHAT, message=SimpleNamespace(id=message_id, message=text))
        asyncio.run(self.signal.on_new(event))


class OwnerGateTest(AppCase):
    def test_first_start_claims_bot(self):
        message = FakeMessage()
        asyncio.run(self.signal.on_start(update(OWNER, message), None))
        self.assertEqual(self.storage.owner_id(), OWNER)
        self.assertEqual(len(message.replies), 1)

    def test_stranger_start_gets_no_answer(self):
        self.own()
        message = FakeMessage()
        asyncio.run(self.signal.on_start(update(99, message), None))
        self.assertEqual(message.replies, [])
        self.assertEqual(self.storage.owner_id(), OWNER)

    def test_stranger_post_is_not_stored(self):
        self.own()
        message = FakeMessage("BTC лонг стоп 78000")
        asyncio.run(self.signal.on_message(update(99, message), None))
        self.assertEqual(message.replies, [])
        self.assertEqual(self.signal.bot.sent, [])
        self.assertEqual(self.storage.stats(), [])

    def test_stranger_cannot_decide(self):
        self.own()
        self.channel_post()
        asyncio.run(self.signal.on_decision(update(99, query=FakeQuery("d:1:take")), None))
        self.assertIsNone(self.storage.get_idea(1)["decision"])

    def test_stranger_commands_are_silent(self):
        self.own()
        asyncio.run(self.signal.on_stats(update(99), None))
        asyncio.run(self.signal.on_digest(update(99), None))
        self.assertEqual(self.signal.bot.sent, [])

    def test_no_owner_means_nobody_is_owner(self):
        self.assertFalse(self.signal.is_owner(update(5)))


class ChannelFlowTest(AppCase):
    def test_idea_card_goes_to_owner_with_buttons(self):
        self.own()
        self.channel_post()
        sent = self.signal.bot.sent[0]
        self.assertEqual(sent.chat_id, OWNER)
        self.assertIn("ЛОНГ BTCUSDT", sent.text)
        self.assertEqual(sent.reply_markup, keyboard(1))
        self.assertEqual(self.storage.get_idea(1)["card_message_id"], 101)

    def test_cursor_moves_with_channel_post(self):
        self.own()
        self.channel_post(message_id=9)
        self.assertEqual(self.storage.cursor(SOURCE.id), 9)

    def test_opinion_sends_nothing_right_away(self):
        self.own()
        self.channel_post(text="Рынок спокойный")
        self.assertEqual(self.signal.bot.sent, [])

    def test_foreign_channel_is_ignored(self):
        self.own()
        event = SimpleNamespace(chat_id=-1009999, message=SimpleNamespace(id=1, message="BTC лонг стоп 78000"))
        asyncio.run(self.signal.on_new(event))
        self.assertEqual(self.signal.bot.sent, [])

    def test_edit_notifies_once_as_reply_to_card(self):
        self.own()
        self.channel_post()
        event = SimpleNamespace(chat_id=EVENT_CHAT, message=SimpleNamespace(id=5, message="стоп перенесён"))
        asyncio.run(self.signal.on_edited(event))
        asyncio.run(self.signal.on_edited(event))
        self.assertEqual(len(self.signal.bot.sent), 2)
        self.assertEqual(self.signal.bot.sent[1].reply_parameters.message_id, 101)

    def test_delete_notifies_and_is_counted(self):
        self.own()
        self.channel_post()
        asyncio.run(self.signal.on_deleted(SimpleNamespace(chat_id=EVENT_CHAT, deleted_ids=[5])))
        self.assertIn("удалил", self.signal.bot.sent[1].text)
        self.assertEqual(self.storage.stats()[0]["deleted"], 1)

    def test_delete_without_chat_is_ignored(self):
        self.own()
        asyncio.run(self.signal.on_deleted(SimpleNamespace(chat_id=None, deleted_ids=[5])))
        self.assertEqual(self.signal.bot.sent, [])


class OwnerActionsTest(AppCase):
    def test_decision_is_saved_and_buttons_change(self):
        self.own()
        self.channel_post()
        query = FakeQuery("d:1:take")
        asyncio.run(self.signal.on_decision(update(OWNER, query=query), None))
        self.assertEqual(self.storage.get_idea(1)["decision"], "take")
        self.assertIn("Беру", query.markups[0].inline_keyboard[0][0].text)

    def test_forwarded_opinion_gets_reply(self):
        self.own()
        message = FakeMessage("Рынок спокойный")
        asyncio.run(self.signal.on_message(update(OWNER, message), None))
        self.assertIn("сводки", message.replies[0])

    def test_forward_of_already_read_post_is_duplicate(self):
        self.own()
        self.channel_post()
        self.assertIsNone(self.storage.add_post("x", origin_key(EVENT_CHAT, 5), None, "t", "idea", T0))

    def test_album_part_without_text_is_silent(self):
        self.own()
        message = FakeMessage("", media_group_id="1")
        asyncio.run(self.signal.on_message(update(OWNER, message), None))
        self.assertEqual(message.replies, [])


if __name__ == "__main__":
    unittest.main()
