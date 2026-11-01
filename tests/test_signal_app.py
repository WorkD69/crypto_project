import asyncio
import inspect
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telegram import Bot, Chat, MessageOriginChannel
from telegram.error import NetworkError
from telethon.tl.types import MessageMediaPhoto, MessageMediaWebPage

from signal_bot.app import SignalBot, keyboard, zone_keyboard
from signal_bot.golden import OWNER_POSTS, OWNER_SET, read_json, set_files
from signal_bot.hermes import BadAnswer, HermesError, Reply
from signal_bot.market import MarketError
from signal_bot.reader import Source, origin_key
from signal_bot.service import ALBUM_PAUSE
from signal_bot.storage import Storage

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
OWNER = 7
SOURCE = Source(1000000001, "Public Author", "example_public")
EVENT_CHAT = -1001000000001
TEXT = "шорт 82000-84000"
PHOTO = b"\xff\xd8\xff" + b"image"


def raw_zone(**changes):
    zone = {"symbol": "BTCUSDT", "side": "SHORT", "zone_low": 82000, "zone_high": 84000, "stop": None,
            "targets": [78000], "timeframe": "4H", "action": "new", "confidence": 0.8, "evidence": "рамка",
            "chart_price": 80000}
    return {**zone, **changes}


def answer(*zones):
    return {"relevant": bool(zones), "zones": list(zones)}


class FakeBot:
    def __init__(self):
        self.sent, self.broken = [], False
        self.files = {}

    async def send_message(self, chat_id, text, **kwargs):
        inspect.signature(Bot.send_message).bind(None, chat_id, text, **kwargs)
        if self.broken:
            raise NetworkError("down")
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, **kwargs))
        return SimpleNamespace(message_id=100 + len(self.sent))

    async def get_file(self, file_id):
        async def download():
            return bytearray(self.files[file_id])
        return SimpleNamespace(download_as_bytearray=download)


class FakeMarket:
    def __init__(self):
        self.broken = False

    async def symbols(self):
        if self.broken:
            raise MarketError("down")
        return {"BTCUSDT", "ETHUSDT"}

    async def price(self, symbol):
        return 80000.0

    async def price_at(self, symbol, moment):
        return 80000.0


class FakeBroker:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    async def ask(self, prompt, image_sha256=None, validate=None, live=True):
        self.calls.append((prompt, image_sha256))
        reply = self.answers.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Reply(reply, {}, 1, validate(reply))


class FakeClient:
    """The Telethon side: channel history and media."""

    def __init__(self):
        self.downloads, self.messages, self.broken = [], {}, False

    async def download_media(self, message, file=None):
        if self.broken:
            raise OSError("down")
        self.downloads.append(message.id)
        return PHOTO + str(message.id).encode()

    async def get_messages(self, entity, ids=None):
        return self.messages.get(ids)

    def iter_messages(self, entity, min_id=0, limit=None):
        async def generate():
            for message in sorted(self.messages.values(), key=lambda item: -item.id):
                if message.id > min_id:
                    yield message
        return generate()


def channel_message(message_id=5, text=TEXT, photo=False, grouped_id=None, media=None):
    return SimpleNamespace(id=message_id, message=text, date=T0, grouped_id=grouped_id,
                           media=MessageMediaPhoto() if photo else media)


class FakeMessage:
    def __init__(self, text="", media_group_id=None, photo=(), forward_origin=None, message_id=50, caption=None):
        self.text, self.caption, self.media_group_id, self.forward_origin = text, caption, media_group_id, forward_origin
        self.photo, self.message_id, self.date = photo, message_id, T0
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


def forwarded(message_id=77, chat_id=EVENT_CHAT, title="Заголовок канала"):
    return MessageOriginChannel(T0, Chat(chat_id, "channel", title=title, username="example_public"), message_id)


class AppCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.spool, self.golden = root / "spool", root / "golden"
        self.spool.mkdir()
        self.golden.mkdir()
        self.storage = Storage(root / "bot.sqlite3")
        self.broker = FakeBroker()
        self.market = FakeMarket()
        self.signal = SignalBot(self.storage, {SOURCE.id: SOURCE}, self.market, self.broker, self.spool, self.golden)
        self.signal.bot = FakeBot()
        self.signal.client = FakeClient()
        self.clock = T0
        clock = patch("signal_bot.app.now_utc", lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)

    def own(self):
        self.storage.claim_owner(OWNER)

    def channel_post(self, *answers, **fields):
        self.broker.answers += answers
        event = SimpleNamespace(chat_id=EVENT_CHAT, message=channel_message(**fields))
        asyncio.run(self.signal.on_new(event))

    def drain(self):
        asyncio.run(self.signal.drain())

    def pass_pause(self):
        self.clock += ALBUM_PAUSE

    def zone_card(self):
        self.own()
        self.channel_post(answer(raw_zone()))
        self.drain()
        return self.storage.get_zone(1)

    def spooled(self):
        return list(self.spool.glob("*.jpg"))

    def texts(self):
        return [item.text for item in self.signal.bot.sent]


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
        self.assertIsNone(self.storage.next_post(T0))

    def test_stranger_cannot_decide(self):
        self.zone_card()
        asyncio.run(self.signal.on_zone_button(update(99, query=FakeQuery("z:1:take")), None))
        self.assertIsNone(self.storage.get_zone(1)["decision"])

    def test_stranger_cannot_start_a_fix(self):
        self.zone_card()
        asyncio.run(self.signal.on_zone_button(update(99, query=FakeQuery("z:1:wrong")), None))
        self.assertIsNone(self.storage.waiting_fix())

    def test_stranger_commands_are_silent(self):
        self.own()
        asyncio.run(self.signal.on_stats(update(99), None))
        asyncio.run(self.signal.on_digest(update(99), None))
        self.assertEqual(self.signal.bot.sent, [])

    def test_no_owner_means_nobody_is_owner(self):
        self.assertFalse(self.signal.is_owner(update(5)))


class ChannelFlowTest(AppCase):
    def test_post_is_safe_before_the_model_is_asked(self):
        self.channel_post()
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(self.storage.next_post(self.clock)["text"], TEXT)

    def test_zone_card_goes_to_owner_with_buttons(self):
        self.own()
        self.channel_post(answer(raw_zone()))
        self.drain()
        sent = self.signal.bot.sent[0]
        self.assertEqual(sent.chat_id, OWNER)
        self.assertIn("ШОРТ BTCUSDT", sent.text)
        self.assertEqual(sent.reply_markup, zone_keyboard(1))
        self.assertEqual(self.storage.get_zone(1)["card_message_id"], 101)

    def test_each_zone_gets_its_own_card(self):
        self.own()
        self.channel_post(answer(raw_zone(), raw_zone(symbol="ETHUSDT", zone_low=78000, zone_high=79000)))
        self.drain()
        self.assertEqual(len(self.signal.bot.sent), 2)

    def test_opinion_sends_nothing_right_away(self):
        self.own()
        self.channel_post(answer(), text="Рынок спокойный")
        self.drain()
        self.assertEqual(self.signal.bot.sent, [])
        self.assertEqual(len(self.storage.posts_since(T0, "opinion")), 1)

    def test_foreign_channel_is_ignored(self):
        self.own()
        event = SimpleNamespace(chat_id=-1009999, message=channel_message())
        asyncio.run(self.signal.on_new(event))
        self.assertIsNone(self.storage.next_post(self.clock))

    def test_photo_goes_to_the_spool_and_the_model(self):
        self.own()
        self.channel_post(answer(raw_zone()), photo=True, text="")
        self.assertEqual(len(self.spooled()), 1)
        self.drain()
        self.assertEqual(self.broker.calls[0][1], self.storage.get_zone(1)["image"])

    def test_spool_is_cleaned_after_reading_and_the_hash_stays(self):
        self.own()
        self.channel_post(answer(raw_zone()), photo=True, text="")
        self.drain()
        self.assertEqual(self.spooled(), [])
        self.assertEqual(len(self.storage.post_images(1)), 1)

    def test_same_image_of_a_waiting_post_stays_in_the_spool(self):
        self.own()
        self.channel_post(answer(), photo=True, text="", message_id=5)
        sha = self.storage.post_images(1)[0]
        self.signal.storage.add_part("A", None, None, "t", T0, None, "x:1", 1, sha, None, None,
                                     T0 + timedelta(days=1), T0)
        self.drain()
        self.assertEqual(len(self.spooled()), 1)

    def test_link_preview_is_not_a_picture(self):
        self.channel_post(media=MessageMediaWebPage(webpage=None))
        self.assertEqual(self.signal.client.downloads, [])

    def test_video_without_a_caption_is_skipped(self):
        self.channel_post(text="", media=object())
        self.assertIsNone(self.storage.next_post(self.clock))

    def test_same_message_is_not_downloaded_twice(self):
        self.channel_post(photo=True, text="")
        self.channel_post(photo=True, text="")
        self.assertEqual(self.signal.client.downloads, [5])

    def test_edit_notifies_once_as_reply_to_card(self):
        self.zone_card()
        event = SimpleNamespace(chat_id=EVENT_CHAT, message=SimpleNamespace(id=5, message="стоп перенесён"))
        asyncio.run(self.signal.on_edited(event))
        asyncio.run(self.signal.on_edited(event))
        self.assertEqual(len(self.signal.bot.sent), 2)
        self.assertEqual(self.signal.bot.sent[1].reply_parameters.message_id, 101)

    def test_delete_notifies_as_reply_to_card(self):
        self.zone_card()
        asyncio.run(self.signal.on_deleted(SimpleNamespace(chat_id=EVENT_CHAT, deleted_ids=[5])))
        self.assertIn("удалил", self.signal.bot.sent[1].text)
        self.assertIsNotNone(self.storage.get_zone(1)["deleted_at"])

    def test_delete_without_chat_is_ignored(self):
        self.own()
        asyncio.run(self.signal.on_deleted(SimpleNamespace(chat_id=None, deleted_ids=[5])))
        self.assertEqual(self.signal.bot.sent, [])


class PollTest(AppCase):
    def setUp(self):
        super().setUp()
        self.storage.set_cursor(SOURCE.id, 4)

    def test_live_message_leaves_the_cursor_to_the_poll(self):
        self.channel_post(message_id=9)
        self.assertEqual(self.storage.cursor(SOURCE.id), 4)

    def test_poll_moves_the_cursor_and_does_not_repeat_a_live_post(self):
        self.channel_post(message_id=9)
        self.signal.client.messages = {9: channel_message(message_id=9)}
        asyncio.run(self.signal.poll_sources())
        self.assertEqual(self.storage.cursor(SOURCE.id), 9)
        self.assertEqual(self.storage.get_post(1)["id"], 1)
        self.assertIsNone(self.storage.get_post(2))

    def test_failed_download_is_retried_by_the_next_poll(self):
        self.signal.client.messages = {5: channel_message(photo=True, text="")}
        self.signal.client.broken = True
        asyncio.run(self.signal.poll_sources())
        self.assertEqual(self.storage.cursor(SOURCE.id), 4)
        self.signal.client.broken = False
        asyncio.run(self.signal.poll_sources())
        self.assertEqual(self.storage.cursor(SOURCE.id), 5)
        self.assertEqual(len(self.storage.post_images(1)), 1)

    def test_album_of_a_poll_is_one_post(self):
        self.signal.client.messages = {5: channel_message(5, "", True, 111), 6: channel_message(6, TEXT, True, 111)}
        asyncio.run(self.signal.poll_sources())
        self.assertEqual(len(self.storage.post_images(1)), 2)
        self.assertEqual(self.storage.get_post(1)["text"], TEXT)


class AlbumTest(AppCase):
    def album(self):
        self.channel_post(message_id=5, photo=True, text="", grouped_id=111)
        self.channel_post(message_id=6, photo=True, text="", grouped_id=111)

    def test_album_without_a_caption_is_read_image_by_image(self):
        self.own()
        self.broker.answers += [answer(raw_zone()), answer(raw_zone(symbol="ETHUSDT"))]
        self.album()
        self.pass_pause()
        self.drain()
        self.assertEqual([call[1] for call in self.broker.calls], self.storage.post_images(1))
        self.assertEqual(len(self.signal.bot.sent), 2)

    def test_album_waits_for_the_pause(self):
        self.album()
        self.drain()
        self.assertEqual(self.broker.calls, [])

    def test_zone_remembers_which_image_showed_it(self):
        self.own()
        self.broker.answers += [answer(), answer(raw_zone())]
        self.album()
        self.pass_pause()
        self.drain()
        self.assertEqual(self.storage.get_zone(1)["image"], self.storage.post_images(1)[1])


class OutageTest(AppCase):
    def fail(self):
        self.own()
        self.channel_post(HermesError("BROKER_DOWN"), photo=True, text="пост")
        self.drain()

    def test_post_goes_out_marked_without_reading(self):
        self.fail()
        self.assertIn("без разбора", self.texts()[0])
        self.assertIn("пост", self.texts()[0])

    def test_notice_is_sent_once(self):
        self.fail()
        self.broker.answers.append(HermesError("BROKER_DOWN"))
        self.clock += timedelta(minutes=5)
        self.drain()
        self.assertEqual(len(self.signal.bot.sent), 1)

    def test_image_waits_in_the_spool(self):
        self.fail()
        self.assertEqual(len(self.spooled()), 1)

    def test_queue_is_read_later_and_the_card_answers_the_notice(self):
        self.fail()
        self.broker.answers.append(answer(raw_zone()))
        self.clock += timedelta(minutes=5)
        self.drain()
        card = self.signal.bot.sent[1]
        self.assertIn("ШОРТ BTCUSDT", card.text)
        self.assertEqual(card.reply_parameters.message_id, 101)
        self.assertEqual(self.spooled(), [])

    def test_late_reading_without_zones_is_told(self):
        self.fail()
        self.broker.answers.append(answer())
        self.clock += timedelta(minutes=5)
        self.drain()
        self.assertIn("Зон нет", self.texts()[1])

    def test_next_post_is_not_blocked(self):
        self.fail()
        self.channel_post(answer(raw_zone()), message_id=6)
        self.drain()
        self.assertIn("ШОРТ", self.texts()[1])

    def test_answers_that_cannot_be_read_end_with_a_message(self):
        self.own()
        self.channel_post(BadAnswer("INVALID_JSON"), BadAnswer("INVALID_JSON"), BadAnswer("INVALID_JSON"), photo=True)
        for _ in range(3):
            self.drain()
            self.clock += timedelta(hours=1)
        self.assertEqual(self.storage.get_post(1)["kind"], "failed")
        self.assertIn("не удалось", self.texts()[-1])
        self.assertEqual(self.spooled(), [])

    def test_bug_in_one_post_does_not_stop_the_next(self):
        self.own()
        self.channel_post(ValueError("bug"))
        self.channel_post(answer(raw_zone()), message_id=6)
        self.drain()
        self.assertTrue(any("ШОРТ" in text for text in self.texts()))

    def test_bybit_without_a_coin_list_postpones_the_post(self):
        self.own()
        self.market.broken = True
        self.channel_post()
        self.drain()
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(self.storage.get_post(1)["last_error"], "NO_SYMBOLS")

    def test_card_survives_a_telegram_failure(self):
        self.own()
        self.channel_post(answer(raw_zone()))
        self.signal.bot.broken = True
        self.drain()
        self.assertIsNone(self.storage.get_zone(1)["card_message_id"])
        self.signal.bot.broken = False
        self.drain()
        self.assertEqual(self.storage.get_zone(1)["card_message_id"], 101)

    def test_card_left_by_a_crash_is_sent_after_restart(self):
        self.channel_post(answer(raw_zone()))
        self.drain()
        self.own()
        self.drain()
        self.assertEqual(len(self.signal.bot.sent), 1)

    def test_two_passes_never_run_together(self):
        self.signal.draining = True
        self.channel_post(answer())
        self.drain()
        self.assertEqual(self.broker.calls, [])


class ZoneNewsTest(AppCase):
    def next_post(self, reply, **fields):
        self.channel_post(reply, message_id=6, **fields)
        self.drain()

    def test_cancel_answers_the_card(self):
        self.zone_card()
        self.next_post(answer(raw_zone(action="cancel", replaces=1)), text="отмена")
        self.assertIn("отменил", self.texts()[1])
        self.assertEqual(self.signal.bot.sent[1].reply_parameters.message_id, 101)

    def test_update_answers_the_card_once(self):
        self.zone_card()
        self.next_post(answer(raw_zone(action="update", replaces=1, zone_low=83000, zone_high=85000)),
                       text="83000-85000")
        self.drain()
        self.assertEqual(len(self.signal.bot.sent), 2)
        self.assertIn("обновил", self.texts()[1])

    def test_unclear_cancel_is_shown(self):
        self.own()
        self.channel_post(answer(raw_zone(), raw_zone(side="LONG")))
        self.drain()
        self.next_post(answer(raw_zone(action="cancel", side=None, zone_low=None, zone_high=None)), text="отмена")
        self.assertIn("какую", self.texts()[-1])


class ForwardTest(AppCase):
    def forward(self, reply=None, **fields):
        self.own()
        if reply is not None:
            self.broker.answers.append(reply)
        message = FakeMessage(**fields)
        asyncio.run(self.signal.on_message(update(OWNER, message), None))
        return message

    def photo_message(self, part, **fields):
        self.signal.bot.files[f"file{part}"] = PHOTO + str(part).encode()
        return dict(photo=(SimpleNamespace(file_id="small"), SimpleNamespace(file_id=f"file{part}")),
                    message_id=50 + part, **fields)

    def test_forward_takes_the_same_path(self):
        message = self.forward(answer(raw_zone()), text=TEXT)
        self.assertIn("Принял", message.replies[0])
        self.drain()
        self.assertEqual(self.signal.bot.sent[0].reply_parameters.message_id, 50)
        self.assertEqual(self.signal.bot.sent[0].reply_markup, zone_keyboard(1))

    def test_forward_without_zones_is_answered(self):
        self.forward(answer(), text="Рынок спокойный")
        self.drain()
        self.assertIn("Зон нет", self.texts()[0])

    def test_forward_of_a_listed_channel_uses_its_name(self):
        self.forward(answer(raw_zone()), text=TEXT, forward_origin=forwarded())
        self.drain()
        self.assertEqual(self.storage.get_zone(1)["author"], SOURCE.name)

    def test_forward_reads_the_author_time(self):
        origin = forwarded()
        self.forward(text=TEXT, forward_origin=origin)
        self.assertEqual(self.storage.next_post(self.clock)["posted_at"], origin.date.isoformat())

    def test_forward_of_an_already_read_post_is_a_duplicate(self):
        self.own()
        self.channel_post(message_id=77)
        message = self.forward(text=TEXT, forward_origin=forwarded())
        self.assertIn("уже есть", message.replies[0])

    def test_album_without_a_caption_makes_one_post_and_one_answer(self):
        first = self.forward(**self.photo_message(1, media_group_id="g"))
        second = self.forward(**self.photo_message(2, media_group_id="g"))
        self.assertEqual(len(first.replies) + len(second.replies), 1)
        self.assertEqual(len(self.storage.post_images(1)), 2)
        self.assertEqual(self.storage.post_parts(1)[0]["file_id"], "file1")

    def test_album_is_read_after_the_pause(self):
        self.broker.answers += [answer(raw_zone()), answer()]
        self.forward(**self.photo_message(1, media_group_id="g"))
        self.forward(**self.photo_message(2, media_group_id="g"))
        self.pass_pause()
        self.drain()
        self.assertEqual(len(self.broker.calls), 2)
        self.assertIn("ШОРТ", self.texts()[0])

    def test_repeated_album_is_answered_once(self):
        self.own()
        self.channel_post(message_id=77)
        self.channel_post(message_id=78)
        replies = [self.forward(text=TEXT, media_group_id="g", forward_origin=forwarded(77), message_id=1).replies,
                   self.forward(text=TEXT, media_group_id="g", forward_origin=forwarded(78), message_id=2).replies]
        self.assertEqual(sum(map(len, replies)), 1)

    def test_nothing_to_read_is_answered(self):
        self.assertIn("нет", self.forward().replies[0])

    def test_album_part_without_content_is_silent(self):
        self.assertEqual(self.forward(media_group_id="1").replies, [])

    def test_full_spool_is_said_aloud(self):
        with patch("signal_bot.app.write_spool", side_effect=__import__("signal_bot.hermes").hermes.SpoolFull("x")):
            message = self.forward(**self.photo_message(1))
        self.assertIn("заполнен", message.replies[0])
        self.assertIsNone(self.storage.next_post(self.clock))


class ButtonsTest(AppCase):
    def press(self, data, user=OWNER):
        query = FakeQuery(data)
        asyncio.run(self.signal.on_zone_button(update(user, query=query), None))
        return query

    def say(self, text):
        message = FakeMessage(text)
        asyncio.run(self.signal.on_message(update(OWNER, message), None))
        return message

    def test_decision_is_saved_and_buttons_change(self):
        self.zone_card()
        query = self.press("z:1:take")
        self.assertEqual(self.storage.get_zone(1)["decision"], "take")
        self.assertIn("Беру", query.markups[0].inline_keyboard[0][0].text)

    def test_wrong_button_stays_after_a_decision(self):
        self.zone_card()
        query = self.press("z:1:skip")
        self.assertEqual(query.markups[0].inline_keyboard[1][0].callback_data, "z:1:wrong")

    def test_old_idea_buttons_keep_working(self):
        self.own()
        post_id = self.storage.add_post("a", None, None, "t", "idea", T0)
        idea_id = self.storage.add_idea(post_id, "BTCUSDT", "LONG", 1.0, 2.0, 0.5, [3.0], False, 2.0, [], T0)
        query = FakeQuery(f"d:{idea_id}:take")
        asyncio.run(self.signal.on_decision(update(OWNER, query=query), None))
        self.assertEqual(self.storage.get_idea(idea_id)["decision"], "take")
        self.assertEqual(keyboard(idea_id).inline_keyboard[0][0].callback_data, f"d:{idea_id}:take")

    def test_wrong_asks_for_the_correction(self):
        self.zone_card()
        query = self.press("z:1:wrong")
        self.assertEqual(self.storage.waiting_fix(), 1)
        self.assertEqual(query.answers, [None])
        self.assertIn("как правильно", self.texts()[-1])
        self.assertEqual(self.signal.bot.sent[-1].reply_parameters.message_id, 101)

    def test_correction_changes_the_zone(self):
        self.zone_card()
        self.press("z:1:wrong")
        self.say("шорт 81000-83000 стоп 85000")
        zone = self.storage.get_zone(1)
        self.assertEqual((zone["zone_low"], zone["zone_high"], zone["stop"]), (81000, 83000, 85000))
        self.assertIsNone(self.storage.waiting_fix())
        self.assertIn("Записал", self.texts()[-1])

    def test_correction_keeps_the_owner_words(self):
        self.zone_card()
        self.press("z:1:wrong")
        self.say("стоп 85000")
        self.assertEqual(self.storage.zone_events(1)[-1]["note"], "стоп 85000")

    def test_correction_becomes_a_reference_label(self):
        self.zone_card()
        self.press("z:1:wrong")
        self.say("81000-83000")
        labels = read_json(set_files(self.golden, OWNER_SET)[0], {})
        self.assertEqual(labels[f"{SOURCE.id}:5"]["zones"][0]["low"], 81000)

    def test_image_comes_back_from_telegram_for_the_reference(self):
        self.own()
        self.channel_post(answer(raw_zone()), photo=True, text="")
        self.drain()
        self.assertEqual(self.spooled(), [])
        self.signal.client.messages[5] = channel_message(photo=True, text="")
        self.press("z:1:wrong")
        self.say("стоп 85000")
        post = read_json(self.golden / OWNER_POSTS, {})[f"{SOURCE.id}:5"]
        self.assertEqual(post["images"], self.storage.post_images(1))
        self.assertTrue((self.golden / f"{post['images'][0]}.jpg").exists())

    def test_forwarded_image_comes_back_by_its_file_id(self):
        self.own()
        self.signal.bot.files["file"] = PHOTO
        self.broker.answers.append(answer(raw_zone()))
        asyncio.run(self.signal.on_message(update(OWNER, FakeMessage(
            photo=(SimpleNamespace(file_id="file"),))), None))
        self.drain()
        self.press("z:1:wrong")
        self.say("стоп 85000")
        self.assertEqual(len(list(self.golden.glob("*.jpg"))), 1)

    def test_lost_image_is_reported_and_the_fix_stays(self):
        self.own()
        self.channel_post(answer(raw_zone()), photo=True, text="")
        self.drain()
        self.signal.client.broken = True
        self.press("z:1:wrong")
        self.say("стоп 85000")
        self.assertEqual(self.storage.get_zone(1)["stop"], 85000)
        self.assertIn("Картинку", self.texts()[-1])

    def test_no_zone_rejects_it(self):
        self.zone_card()
        self.press("z:1:wrong")
        self.say("нет")
        self.assertEqual(self.storage.get_zone(1)["status"], "REJECTED")
        self.assertEqual(read_json(set_files(self.golden, OWNER_SET)[0], {})[f"{SOURCE.id}:5"]["zones"], [])

    def test_cancel_leaves_the_zone_alone(self):
        self.zone_card()
        self.press("z:1:wrong")
        message = self.say("отмена")
        self.assertIsNone(self.storage.waiting_fix())
        self.assertEqual(self.storage.get_zone(1)["version"], 1)
        self.assertIn("отменил", message.replies[0].lower())

    def test_unclear_words_ask_again(self):
        self.zone_card()
        self.press("z:1:wrong")
        message = self.say("что-то не то")
        self.assertEqual(self.storage.waiting_fix(), 1)
        self.assertIn("как правильно", message.replies[0])

    def test_forward_during_a_fix_stays_a_post(self):
        self.zone_card()
        self.press("z:1:wrong")
        asyncio.run(self.signal.on_message(update(OWNER, FakeMessage(TEXT, forward_origin=forwarded(90))), None))
        self.assertEqual(self.storage.waiting_fix(), 1)
        self.assertIsNotNone(self.storage.next_post(self.clock))

    def test_correction_is_not_sent_to_the_model(self):
        self.zone_card()
        self.press("z:1:wrong")
        self.say("стоп 85000")
        self.assertIsNone(self.storage.next_post(self.clock))


class CommandsTest(AppCase):
    def test_open_shows_zones(self):
        self.zone_card()
        asyncio.run(self.signal.on_open(update(OWNER), None))
        self.assertIn("Открытые зоны", self.texts()[-1])

    def test_digest_shows_zones_and_the_queue(self):
        self.zone_card()
        self.channel_post(message_id=6)
        asyncio.run(self.signal.on_digest(update(OWNER), None))
        self.assertIn("BTCUSDT", self.texts()[-1])
        self.assertIn("Без разбора: 1", self.texts()[-1])

    def test_forward_key_matches_the_reader(self):
        self.assertEqual(origin_key(EVENT_CHAT, 5), f"{SOURCE.id}:5")


if __name__ == "__main__":
    unittest.main()
