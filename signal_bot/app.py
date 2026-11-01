"""Telegram layer: owner-only bot plus the channel reader in one asyncio loop."""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, MessageOriginChannel, \
    MessageOriginChat, MessageOriginHiddenUser, MessageOriginUser, ReplyParameters, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telethon import TelegramClient, events
from telethon.tl.types import PeerChannel

from signal_bot.cards import chunks, render_delete, render_digest, render_edit, render_event, render_open, \
    render_stats, render_unclear, render_unparsed, render_zone, render_zone_delete, render_zone_edit, render_zone_event
from signal_bot.fixes import FIX_HELP, REJECT, parse_fix
from signal_bot.golden import save_fix
from signal_bot.hermes import BrokerClient, HermesError, SpoolFull, write_spool
from signal_bot.market import BybitClient
from signal_bot.reader import catch_up, has_photo, load_sources, origin_key, plain_id, post_from, post_url
from signal_bot.service import Incoming, SymbolCache, accept, analyze_post, next_try, track_open
from signal_bot.storage import Storage

log = logging.getLogger("signal_bot")
MOSCOW = ZoneInfo("Europe/Moscow")
DIGEST_AT = time(21, 0, tzinfo=MOSCOW)
TRACK_EVERY = 300
POLL_EVERY = 300
PARSE_EVERY = 3
HELP = ("Готово, бот работает только с тобой.\n\n"
        "Посты авторов я читаю сам: зоны приходят карточками, остальное собирается в сводку в 21:00 по Москве.\n"
        "Любой пост и альбом можно переслать и вручную.\n\n"
        "/open открытые зоны\n/stats статистика по авторам\n/digest сводка прямо сейчас")
NO_ZONES = "Зон нет, сохранил для вечерней сводки."
GAVE_UP = "Разобрать пост не удалось, он остаётся без разбора."
FIX_ASK = "Что неверно в зоне? " + FIX_HELP


def origin_of(message) -> tuple[str, str | None, str | None]:
    origin = message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        chat = origin.chat
        return (chat.title or "Канал", origin_key(chat.id, origin.message_id),
                post_url(chat.id, chat.username, origin.message_id))
    if isinstance(origin, MessageOriginChat):
        return origin.sender_chat.title or "Чат", None, None
    if isinstance(origin, MessageOriginUser):
        return origin.sender_user.full_name, None, None
    if isinstance(origin, MessageOriginHiddenUser):
        return origin.sender_user_name, None, None
    return "Ты", None, None


def keyboard(idea_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Беру", callback_data=f"d:{idea_id}:take"),
                                  InlineKeyboardButton("Пропускаю", callback_data=f"d:{idea_id}:skip")]])


def zone_keyboard(zone_id: int, decision: str | None = None) -> InlineKeyboardMarkup:
    if decision:
        word = "Беру" if decision == "take" else "Пропускаю"
        top = [InlineKeyboardButton(f"Решение: {word}", callback_data="noop")]
    else:
        top = [InlineKeyboardButton("Беру", callback_data=f"z:{zone_id}:take"),
               InlineKeyboardButton("Пропускаю", callback_data=f"z:{zone_id}:skip")]
    return InlineKeyboardMarkup([top, [InlineKeyboardButton("Неверно", callback_data=f"z:{zone_id}:wrong")]])


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class SignalBot:
    def __init__(self, storage: Storage, sources: dict, market: BybitClient, broker, spool: Path, golden: Path):
        self.storage, self.sources, self.market, self.broker = storage, sources, market, broker
        self.spool, self.golden = spool, golden
        self.symbols = SymbolCache(market)
        self.client: TelegramClient | None = None
        self.bot = None
        self.draining = False
        self.answered_albums: set[str] = set()

    def is_owner(self, update: Update) -> bool:
        return update.effective_user is not None and update.effective_user.id == self.storage.owner_id()

    async def send(self, text: str, reply_to: int | None = None, markup=None):
        owner = self.storage.owner_id()
        if owner is None:
            return None
        reply = ReplyParameters(reply_to, allow_sending_without_reply=True) if reply_to else None
        sent = None
        for part in chunks(text):
            sent = await self.bot.send_message(owner, part, parse_mode=ParseMode.HTML, reply_markup=markup,
                                               reply_parameters=reply,
                                               link_preview_options=LinkPreviewOptions(is_disabled=True))
        return sent

    async def deliver(self) -> None:
        """Cards and author news are stored first and sent from here, so a failure only delays them."""
        for zone in self.storage.unsent_zones():
            sent = await self.send(render_zone(zone), reply_to=zone["reply_to"] or zone["notice_id"],
                                   markup=zone_keyboard(zone["id"]))
            if sent:
                self.storage.set_zone_card(zone["id"], sent.message_id)
        for event in self.storage.unsent_events():
            zone = self.storage.get_zone(event["zone_id"])
            post = self.storage.get_post(event["post_id"])
            if await self.send(render_zone_event(zone, event, post["url"]), reply_to=zone["card_message_id"]):
                self.storage.event_sent(event["id"], now_utc())

    def drop_images(self, post_id: int) -> None:
        waiting = self.storage.waiting_images()
        for sha in self.storage.post_images(post_id):
            if sha not in waiting:
                (self.spool / f"{sha}.jpg").unlink(missing_ok=True)

    async def on_parse_error(self, post: dict, error: Exception) -> None:
        code = error.code if isinstance(error, HermesError) else type(error).__name__
        log.warning("post %s not parsed: %s", post["id"], code)
        retry_at = next_try(post, error, now_utc())
        if retry_at:
            self.storage.postpone_post(post["id"], retry_at, code)
        else:
            self.storage.give_up_post(post["id"], code)
            self.drop_images(post["id"])
        if post["notice_id"] is None:
            sent = await self.send(render_unparsed(post, len(self.storage.post_images(post["id"]))),
                                   reply_to=post["reply_to"])
            if sent:
                self.storage.set_notice(post["id"], sent.message_id)
        elif retry_at is None:
            await self.send(GAVE_UP, reply_to=post["notice_id"])

    async def parse(self, post: dict) -> None:
        symbols = await self.symbols.get(now_utc())
        try:
            if not symbols:
                raise HermesError("NO_SYMBOLS")
            result = await analyze_post(self.storage, self.broker, self.market, post, symbols, now_utc())
        except Exception as exc:
            await self.on_parse_error(post, exc)
            return
        self.drop_images(post["id"])
        answer_to = post["reply_to"] or post["notice_id"]
        if result.kind == "opinion" and answer_to:
            await self.send(NO_ZONES, reply_to=answer_to)
        for zone in result.unclear:
            await self.send(render_unclear(zone, post["url"]), reply_to=answer_to)

    async def drain(self) -> None:
        if self.draining:
            return
        self.draining = True
        try:
            await self.deliver()
            while post := self.storage.next_post(now_utc()):
                await self.parse(post)
                await self.deliver()
        except TelegramError as exc:
            log.warning("delivery failed: %s", type(exc).__name__)
        finally:
            self.draining = False

    async def store_source_message(self, source, message) -> None:
        post, photo = post_from(source, message), has_photo(message)
        if (photo or post.text.strip()) and not self.storage.has_part(post.part_key):
            image = write_spool(self.spool, await self.client.download_media(message, file=bytes)) if photo else None
            accept(self.storage, post, image, now_utc())

    async def on_source_message(self, source, message) -> None:
        await self.store_source_message(source, message)
        self.storage.set_cursor(source.id, message.id)

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.storage.claim_owner(update.effective_user.id):
            await update.effective_message.reply_text(HELP)

    def incoming(self, message) -> Incoming:
        author, key, url = origin_of(message)
        origin = message.forward_origin
        if isinstance(origin, MessageOriginChannel) and plain_id(origin.chat.id) in self.sources:
            author = self.sources[plain_id(origin.chat.id)].name
        group = message.media_group_id
        return Incoming(author, key, url, message.text or message.caption or "", origin.date if origin else message.date,
                        key or f"chat:{message.message_id}", message.message_id, f"chat:{group}" if group else None,
                        message.photo[-1].file_id if message.photo else None, message.message_id)

    async def answer_album_once(self, message, text: str) -> None:
        group = message.media_group_id
        if group not in self.answered_albums:
            await message.reply_text(text)
        if group:
            self.answered_albums.add(group)

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_owner(update):
            return
        message = update.effective_message
        waiting = self.storage.waiting_fix()
        if waiting is not None and message.text and message.forward_origin is None:
            await self.on_fix(message, waiting)
            return
        post = self.incoming(message)
        if not post.text.strip() and not post.file_id:
            if not message.media_group_id:
                await message.reply_text("В сообщении нет ни текста, ни картинки.")
            return
        if self.storage.has_part(post.part_key):
            await self.answer_album_once(message, "Этот пост у меня уже есть.")
            return
        image = None
        if post.file_id:
            try:
                image = write_spool(self.spool, await self.file_bytes(post.file_id))
            except SpoolFull:
                await message.reply_text("Спул картинок заполнен, пришли пост позже.")
                return
        if accept(self.storage, post, image, now_utc()).kind == "queued":
            await message.reply_text("Принял, разбираю.")

    async def on_decision(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self.is_owner(update):
            await query.answer()
            return
        _, idea_id, decision = query.data.split(":")
        self.storage.set_decision(int(idea_id), decision)
        await query.answer("Записал")
        chosen = self.storage.get_idea(int(idea_id))["decision"]
        word = "Беру" if chosen == "take" else "Пропускаю"
        await query.edit_message_reply_markup(
            InlineKeyboardMarkup([[InlineKeyboardButton(f"Решение: {word}", callback_data="noop")]]))

    async def on_zone_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self.is_owner(update):
            await query.answer()
            return
        _, zone_id, action = query.data.split(":")
        zone = self.storage.get_zone(int(zone_id))
        if action == "wrong":
            self.storage.set_waiting_fix(zone["id"])
            await query.answer()
            await self.send(FIX_ASK, reply_to=zone["card_message_id"])
            return
        self.storage.set_zone_decision(zone["id"], action)
        await query.answer("Записал")
        await query.edit_message_reply_markup(zone_keyboard(zone["id"], self.storage.get_zone(zone["id"])["decision"]))

    async def file_bytes(self, file_id: str) -> bytes:
        file = await self.bot.get_file(file_id)
        return bytes(await file.download_as_bytearray())

    async def part_image(self, part: dict) -> bytes:
        """The spool is emptied after reading, so a reference image comes back from Telegram."""
        if part["file_id"]:
            return await self.file_bytes(part["file_id"])
        source_id, message_id = map(int, part["part_key"].split(":"))
        if source_id not in self.sources:
            raise ValueError("source is not allowed")
        message = await self.client.get_messages(PeerChannel(source_id), ids=message_id)
        return await self.client.download_media(message, file=bytes)

    async def remember_fix(self, post_id: int) -> bool:
        post, images, complete = self.storage.get_post(post_id), [], True
        for part in self.storage.post_parts(post_id):
            if not part["image_sha"]:
                continue
            try:
                data = await self.part_image(part)
                sha = hashlib.sha256(data).hexdigest()
                (self.golden / f"{sha}.jpg").write_bytes(data)
                images.append(sha)
            except Exception as exc:
                log.warning("reference image of post %s lost: %s", post_id, type(exc).__name__)
                complete = False
        save_fix(self.golden, {"key": post["origin_key"] or f"post:{post_id}", "author": post["author"],
                               "url": post["url"], "date": post["posted_at"], "images": images,
                               "text": post["edited_text"] or post["text"]}, self.storage.zones_of_post(post_id))
        return complete

    async def on_fix(self, message, zone_id: int) -> None:
        text = message.text.strip()
        if text.lower() == "отмена":
            self.storage.set_waiting_fix(None)
            await message.reply_text("Отменил, зона осталась как была.")
            return
        fix = parse_fix(text, await self.symbols.get(now_utc()))
        if fix is None:
            await message.reply_text(FIX_ASK)
            return
        if fix is REJECT:
            self.storage.reject_zone(zone_id, text, now_utc())
        else:
            self.storage.fix_zone(zone_id, fix, text, now_utc())
        self.storage.set_waiting_fix(None)
        zone = self.storage.get_zone(zone_id)
        saved = await self.remember_fix(zone["post_id"])
        reply = "Записал: зоны здесь нет." if fix is REJECT else f"Записал правку.\n\n{render_zone(zone)}"
        if not saved:
            reply += "\n\nКартинку в эталон сохранить не удалось."
        await self.send(reply, reply_to=zone["card_message_id"])

    async def on_noop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.callback_query.answer()

    async def on_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.is_owner(update):
            await self.send(render_stats(self.storage.stats()))

    async def on_open(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.is_owner(update):
            await self.send(render_open(self.storage.open_ideas(), self.storage.open_zones()))

    async def send_digest(self) -> None:
        since = now_utc() - timedelta(days=1)
        await self.send(render_digest(self.storage.posts_since(since, "opinion"), self.storage.ideas_since(since),
                                      self.storage.zones_since(since), self.storage.count_unparsed()))

    async def on_digest(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.is_owner(update):
            await self.send_digest()

    async def job_digest(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.send_digest()

    async def job_track(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        for change in await track_open(self.storage, self.market, now_utc()):
            await self.send(render_event(change.idea, change.trade), reply_to=change.idea["card_message_id"])

    async def job_poll(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.poll_sources()

    async def job_parse(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.drain()

    async def poll_sources(self) -> None:
        """Live updates can lag for big channels; polling guarantees no post is skipped."""
        for source in self.sources.values():
            try:
                latest = await catch_up(self.client, source, self.storage.cursor(source.id), self.on_source_message)
            except Exception as exc:
                log.warning("poll failed for source %s: %s", source.id, type(exc).__name__)
                continue
            if latest is not None:
                self.storage.set_cursor(source.id, latest)

    async def on_new(self, event) -> None:
        """Only the poll moves the cursor: a live post that fails here is picked up by it."""
        source = self.sources.get(plain_id(event.chat_id))
        if source:
            await self.store_source_message(source, event.message)

    async def on_edited(self, event) -> None:
        source = self.sources.get(plain_id(event.chat_id))
        if not source or not event.message.message:
            return
        text = event.message.message.strip()
        changed = self.storage.record_edit(origin_key(source.id, event.message.id), text, now_utc())
        if changed and changed["idea"]:
            await self.send(render_edit(changed["idea"], text), reply_to=changed["idea"]["card_message_id"])
        if changed and changed["zones"]:
            await self.send(render_zone_edit(changed["zones"], text), reply_to=changed["zones"][0]["card_message_id"])

    async def on_deleted(self, event) -> None:
        source = self.sources.get(plain_id(event.chat_id)) if event.chat_id else None
        if not source:
            return
        for message_id in event.deleted_ids:
            changed = self.storage.record_delete(origin_key(source.id, message_id), now_utc())
            if changed and changed["idea"]:
                await self.send(render_delete(changed["idea"]), reply_to=changed["idea"]["card_message_id"])
            if changed and changed["zones"]:
                await self.send(render_zone_delete(changed["zones"]), reply_to=changed["zones"][0]["card_message_id"])

    async def start_reader(self, session: Path, api_id: int, api_hash: str) -> None:
        self.client = TelegramClient(str(session), api_id, api_hash)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("READER_SESSION_NOT_AUTHORIZED")
        chats = [PeerChannel(source_id) for source_id in self.sources]
        self.client.add_event_handler(self.on_new, events.NewMessage(chats=chats))
        self.client.add_event_handler(self.on_edited, events.MessageEdited(chats=chats))
        self.client.add_event_handler(self.on_deleted, events.MessageDeleted(chats=chats))
        await self.poll_sources()


def build(data_dir: Path, config_dir: Path) -> Application:
    storage = Storage(data_dir / "bot.sqlite3")
    sources = load_sources((config_dir / "sources.json").read_text(encoding="utf-8"))
    golden = data_dir / "golden"
    golden.mkdir(exist_ok=True)
    signal = SignalBot(storage, sources, BybitClient(), BrokerClient(), data_dir / "spool", golden)

    async def post_init(application: Application) -> None:
        signal.bot = application.bot
        await signal.start_reader(data_dir / "reader", int(os.environ["TELEGRAM_API_ID"]),
                                  os.environ["TELEGRAM_API_HASH"])
        queue = application.job_queue
        queue.run_repeating(signal.job_track, TRACK_EVERY, first=30)
        queue.run_repeating(signal.job_poll, POLL_EVERY, first=POLL_EVERY)
        # A long reading overlaps the next tick on purpose: drain() lets only one pass run.
        queue.run_repeating(signal.job_parse, PARSE_EVERY, first=PARSE_EVERY, job_kwargs={"max_instances": 2})
        queue.run_daily(signal.job_digest, DIGEST_AT)

    async def post_shutdown(application: Application) -> None:
        if signal.client:
            await signal.client.disconnect()

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("handler failed: %s", type(context.error).__name__)

    application = (Application.builder().token(os.environ["BOT_TOKEN"]).post_init(post_init)
                   .post_shutdown(post_shutdown).build())
    private = filters.ChatType.PRIVATE
    application.add_handler(CommandHandler("start", signal.on_start, private))
    application.add_handler(CommandHandler("stats", signal.on_stats, private))
    application.add_handler(CommandHandler("open", signal.on_open, private))
    application.add_handler(CommandHandler("digest", signal.on_digest, private))
    application.add_handler(CallbackQueryHandler(signal.on_decision, pattern=r"^d:\d+:(take|skip)$"))
    application.add_handler(CallbackQueryHandler(signal.on_zone_button, pattern=r"^z:\d+:(take|skip|wrong)$"))
    application.add_handler(CallbackQueryHandler(signal.on_noop, pattern=r"^noop$"))
    application.add_handler(MessageHandler(private & ~filters.COMMAND, signal.on_message))
    application.add_error_handler(on_error)
    return application


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full URLs at INFO, and Bot API URLs contain the token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    data_dir = Path(os.environ.get("BOT_DATA_DIR", "/var/lib/crypto-signals"))
    config_dir = Path(os.environ.get("BOT_CONFIG_DIR", "/etc/crypto-signals"))
    build(data_dir, config_dir).run_polling(allowed_updates=Update.ALL_TYPES)
