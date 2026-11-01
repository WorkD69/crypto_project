"""One-time interactive login of the reader account. Run by hand on the server."""
import asyncio
import json
import os
from getpass import getpass
from pathlib import Path

from telethon import TelegramClient
from telethon.tl.types import Channel, User

DATA = Path(os.environ.get("BOT_DATA_DIR", "/var/lib/crypto-signals"))


async def main():
    client = TelegramClient(str(DATA / "reader"), int(os.environ["TELEGRAM_API_ID"]), os.environ["TELEGRAM_API_HASH"])
    await client.start(phone=lambda: input("Номер второго аккаунта (+7...): ").strip(),
                       code_callback=lambda: input("Код из Telegram: ").strip(),
                       password=lambda: getpass("Пароль 2FA, если есть: "))
    me = await client.get_me()
    sources = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, Channel) and entity.broadcast:
            sources.append({"id": entity.id, "kind": "channel", "title": dialog.title, "username": entity.username})
        elif isinstance(entity, User) and entity.bot:
            sources.append({"id": entity.id, "kind": "bot", "title": dialog.title, "username": entity.username})
    (DATA / "dialogs.json").write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nВход выполнен: {me.first_name}. Найдено каналов и ботов: {len(sources)}.")
    await client.disconnect()


asyncio.run(main())
