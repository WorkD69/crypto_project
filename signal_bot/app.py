"""Telegram layer: owner-only bot plus the channel reader in one asyncio loop."""
from __future__ import annotations

import logging
import os
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, MessageOriginChannel, \
    MessageOriginChat, MessageOriginHiddenUser, MessageOriginUser, ReplyParameters, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telethon import TelegramClient, events
from telethon.tl.types import PeerChannel

from signal_bot.cards import chunks, render_card, render_delete, render_digest, render_edit, render_event, \
    render_open, render_stats
from signal_bot.market import BybitClient
from signal_bot.reader import catch_up, load_sources, origin_key, plain_id, post_from, post_url
from signal_bot.service import Incoming, SymbolCache, process_post, track_open
from signal_bot.storage import Storage

log = logging.getLogger("signal_bot")
MOSCOW = ZoneInfo("Europe/Moscow")
DIGEST_AT = time(21, 0, tzinfo=MOSCOW)
TRACK_EVERY = 300
POLL_EVERY = 300
HELP = ("Готово, бот работает только с тобой.\n\n"
        "Посты авторов я читаю сам: идеи приходят сразу, мнения собираются в сводку в 21:00 по Москве.\n"
        "Любой пост можно переслать и вручную.\n\n"
        "/open открытые идеи\n/stats статистика по авторам\n/digest сводка прямо сейчас")


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


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class SignalBot:
    def __init__(self, storage: Storage, sources: dict, market: BybitClient):
        self.storage, self.sources, self.market = storage, sources, market
        self.symbols = SymbolCache(market)
        self.client: TelegramClient | None = None
        self.bot = None

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

    async def deliver(self, idea_id: int) -> None:
        sent = await self.send(render_card(self.storage.get_idea(idea_id)), markup=keyboard(idea_id))
        if sent:
            self.storage.set_card_message(idea_id, sent.message_id)

    async def ingest(self, post: Incoming):
        now = now_utc()
        outcome = await process_post(self.storage, self.market, post, await self.symbols.get(now), now)
        if outcome.kind == "idea":
            await self.deliver(outcome.idea_id)
        return outcome

    async def on_source_message(self, source, message) -> None:
        await self.ingest(post_from(source, message))
        self.storage.set_cursor(source.id, message.id)

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.storage.claim_owner(update.effective_user.id):
            await update.effective_message.reply_text(HELP)

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_owner(update):
            return
        message = update.effective_message
        author, key, url = origin_of(message)
        outcome = await self.ingest(Incoming(author, key, url, message.text or message.caption or ""))
        if outcome.kind == "opinion":
            await message.reply_text("Идеи с уровнями тут нет, сохранил для вечерней сводки.")
        elif outcome.kind == "duplicate":
            await message.reply_text("Этот пост у меня уже есть.")
        elif outcome.kind == "empty" and not message.media_group_id:
            await message.reply_text("В сообщении нет текста.")

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

    async def on_noop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.callback_query.answer()

    async def on_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.is_owner(update):
            await self.send(render_stats(self.storage.stats()))

    async def on_open(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.is_owner(update):
            await self.send(render_open(self.storage.open_ideas()))

    async def send_digest(self) -> None:
        since = now_utc() - timedelta(days=1)
        await self.send(render_digest(self.storage.posts_since(since, "opinion"), self.storage.ideas_since(since)))

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
        source = self.sources.get(plain_id(event.chat_id))
        if source:
            await self.on_source_message(source, event.message)

    async def on_edited(self, event) -> None:
        source = self.sources.get(plain_id(event.chat_id))
        if not source or not event.message.message:
            return
        text = event.message.message.strip()
        idea = self.storage.record_edit(origin_key(source.id, event.message.id), text, now_utc())
        if idea:
            await self.send(render_edit(idea, text), reply_to=idea["card_message_id"])

    async def on_deleted(self, event) -> None:
        source = self.sources.get(plain_id(event.chat_id)) if event.chat_id else None
        if not source:
            return
        for message_id in event.deleted_ids:
            idea = self.storage.record_delete(origin_key(source.id, message_id), now_utc())
            if idea:
                await self.send(render_delete(idea), reply_to=idea["card_message_id"])

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
    signal = SignalBot(storage, sources, BybitClient())

    async def post_init(application: Application) -> None:
        signal.bot = application.bot
        await signal.start_reader(data_dir / "reader", int(os.environ["TELEGRAM_API_ID"]),
                                  os.environ["TELEGRAM_API_HASH"])
        queue = application.job_queue
        queue.run_repeating(signal.job_track, TRACK_EVERY, first=30)
        queue.run_repeating(signal.job_poll, POLL_EVERY, first=POLL_EVERY)
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
