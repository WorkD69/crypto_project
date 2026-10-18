"""Interactive, read-only Telethon probe for a source you own or may test.

Run yourself in a private local terminal. Never paste credentials into chat.
"""
import argparse
import asyncio
import getpass
import json
import os
import time
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--peer", required=True, help="Own channel username or 'me' for Saved Messages")
    parser.add_argument("--watch-seconds", type=int, default=30)
    args = parser.parse_args()
    if not 0 <= args.watch_seconds <= 120:
        parser.error("watch-seconds must be 0..120")
    folder = Path(os.environ["LOCALAPPDATA"]) / "CryptoResearchProbe"
    folder.mkdir(parents=True, exist_ok=True)
    api_id = int(getpass.getpass("Telegram API ID (hidden): "))
    api_hash = getpass.getpass("Telegram API hash (hidden): ")
    client = TelegramClient(str(folder / "own_source"), api_id, api_hash)
    client.session.save_entities = False
    await client.connect()
    try:
        if not await client.is_user_authorized():
            phone = getpass.getpass("Second-account phone (hidden): ")
            await client.send_code_request(phone)
            try:
                await client.sign_in(phone, getpass.getpass("Login code (hidden): "))
            except SessionPasswordNeededError:
                await client.sign_in(password=getpass.getpass("2FA password (hidden): "))
        peer = await client.get_entity(args.peer)
        started = time.perf_counter()
        messages = [m async for m in client.iter_messages(peer, limit=20)]
        elapsed = round(time.perf_counter() - started, 3)
        # Message bodies, captions, filenames, user IDs and credentials are deliberately omitted.
        summary = []
        for m in messages:
            public_link = (f"https://t.me/{peer.username}/{m.id}"
                           if args.peer != "me" and getattr(peer, "username", None) else None)
            summary.append({"id": m.id, "date_utc": m.date.isoformat(), "public_link": public_link,
                            "has_text_or_caption": bool(m.message), "photo": bool(m.photo),
                            "video": bool(m.video), "document": bool(m.document),
                            "album_grouped_id": m.grouped_id, "button_rows": len(m.buttons or []),
                            "sender_present": m.sender_id is not None})
        print(json.dumps({"history_count": len(summary), "history_seconds": elapsed,
                          "messages": summary}, ensure_ascii=True))
        seen = []
        @client.on(events.NewMessage(chats=peer))
        async def on_new(event):
            seen.append(event.message.id)
        if args.watch_seconds:
            print(f"Watching own source for {args.watch_seconds} seconds; no messages sent.")
            await asyncio.sleep(args.watch_seconds)
        print(json.dumps({"new_message_ids": seen, "watch_seconds": args.watch_seconds}))
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
