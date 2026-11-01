"""Read-only access to an explicit allowlist of channels through Telethon."""
from __future__ import annotations

import json
from dataclasses import dataclass

from telethon.tl.types import MessageMediaPhoto, PeerChannel

from signal_bot.service import Incoming

CATCH_UP_LIMIT = 100


@dataclass(frozen=True)
class Source:
    id: int
    name: str
    username: str | None


def load_sources(text: str) -> dict[int, Source]:
    return {row["id"]: Source(row["id"], row["name"], row.get("username")) for row in json.loads(text)}


def plain_id(chat_id: int) -> int:
    """Bot API and Telethon events prefix channel ids with -100."""
    return int(str(chat_id).removeprefix("-100"))


def origin_key(chat_id: int, message_id: int) -> str:
    return f"{plain_id(chat_id)}:{message_id}"


def post_url(chat_id: int, username: str | None, message_id: int) -> str:
    return f"https://t.me/{username}/{message_id}" if username else f"https://t.me/c/{plain_id(chat_id)}/{message_id}"


def has_photo(message) -> bool:
    """Not message.photo: Telethon also returns the preview picture of a link there."""
    return isinstance(message.media, MessageMediaPhoto)


def post_from(source: Source, message) -> Incoming:
    key = origin_key(source.id, message.id)
    album = f"{source.id}:{message.grouped_id}" if message.grouped_id else None
    return Incoming(source.name, key, post_url(source.id, source.username, message.id), message.message or "",
                    message.date, key, message.id, album)


async def catch_up(client, source: Source, cursor: int | None, handle) -> int | None:
    """Handle posts newer than the cursor; the first run only marks the current end."""
    entity = PeerChannel(source.id)
    if cursor is None:
        latest = [message async for message in client.iter_messages(entity, limit=1)]
        return latest[0].id if latest else None
    messages = [message async for message in client.iter_messages(entity, min_id=cursor, limit=CATCH_UP_LIMIT)]
    for message in sorted(messages, key=lambda item: item.id):
        await handle(source, message)
    return max((message.id for message in messages), default=cursor)
