"""Reference set of labeled posts: collect them, run the model, measure the reading of charts.

The folder lives on the server and never gets into Git: it holds author content.

    python -m signal_bot.golden collect   (crypto-signals must be stopped: the session is shared)
    python -m signal_bot.golden run       (reads labels.json, can resume)
    python -m signal_bot.golden score
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path

from signal_bot.broker import SPOOL
from signal_bot.extract import extract_post
from signal_bot.hermes import BrokerClient, HermesError, write_spool
from signal_bot.market import BybitClient
from signal_bot.reader import load_sources, origin_key, post_url

TOLERANCE = 0.015
MIN_COIN_SIDE = 0.9
MIN_BOUNDS = 0.8
EPSILON = 1e-9
SCAN_LIMIT = 400
LIVE_ACTIONS = {"new", "update"}


def zone_matches(label: dict, found: dict) -> bool:
    low, high = found.get("zone_low"), found.get("zone_high")
    if low is None or high is None:
        return False
    return (abs(low - label["low"]) / label["low"] <= TOLERANCE + EPSILON and
            abs(high - label["high"]) / label["high"] <= TOLERANCE + EPSILON)


def _ticker(zone: dict) -> str:
    return zone["symbol"].split(":")[-1].upper()


def _pair(labels: list[dict], found: list[dict]) -> tuple[int, int, int]:
    """Exact readings claim their predictions first, so a neighbour cannot steal them."""
    free, unmatched, coin_side, bounds = list(found), [], 0, 0
    same = lambda label, zone: (_ticker(zone), zone["side"]) == (_ticker(label), label["side"])
    for label in labels:
        hit = next((zone for zone in free if same(label, zone) and zone_matches(label, zone)), None)
        if hit is None:
            unmatched.append(label)
            continue
        free.remove(hit)
        coin_side, bounds = coin_side + 1, bounds + 1
    for label in unmatched:
        hit = next((zone for zone in free if same(label, zone)), None)
        if hit is not None:
            free.remove(hit)
            coin_side += 1
    return coin_side, bounds, len(free)


def score(cases: list[dict]) -> dict:
    total = coin_side = bounds = extra = false_alarms = 0
    for case in cases:
        found = [zone for zone in case["predicted"] if zone.get("action", "new") in LIVE_ACTIONS]
        hits, exact, unused = _pair(case["labels"], found)
        total += len(case["labels"])
        coin_side, bounds, extra = coin_side + hits, bounds + exact, extra + unused
        false_alarms += bool(found) and not case["labels"]
    report = {"zones": total, "coin_side": coin_side / total if total else 0.0, "bounds": bounds / total if total else 0.0,
              "extra": extra, "false_alarm_posts": false_alarms}
    report["passed"] = bool(total) and report["coin_side"] >= MIN_COIN_SIDE and report["bounds"] >= MIN_BOUNDS
    return report


def group_album(messages: list) -> list[list]:
    """Posts with pictures, newest first; an album becomes one post with its parts in order."""
    with_photo = [message for message in messages if message.photo]
    key = lambda message: message.grouped_id or -message.id
    posts = [sorted(parts, key=lambda part: part.id) for _, parts in groupby(sorted(with_photo, key=key), key)]
    return sorted(posts, key=lambda parts: parts[0].id, reverse=True)


def set_files(folder: Path, name: str) -> tuple[Path, Path]:
    suffix = f"-{name}" if name else ""
    return folder / f"labels{suffix}.json", folder / f"results{suffix}.json"


def read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json(path: Path, value) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, path)


async def collect(folder: Path, config: Path, per_source: int, days: int) -> None:
    from telethon import TelegramClient
    from telethon.tl.types import PeerChannel

    sources = load_sources((config / "sources.json").read_text(encoding="utf-8"))
    client = TelegramClient(str(folder.parent / "reader"), int(os.environ["TELEGRAM_API_ID"]),
                            os.environ["TELEGRAM_API_HASH"])
    since, posts = datetime.now(timezone.utc) - timedelta(days=days), []
    await client.connect()
    try:
        for source in sources.values():
            found = []
            async for message in client.iter_messages(PeerChannel(source.id), limit=SCAN_LIMIT):
                if message.date < since:
                    break
                found.append(message)
            albums = group_album(found)
            print(source.id, "scanned", len(found), "with pictures", len(albums))
            for parts in albums[:per_source]:
                images = []
                for part in parts:
                    data = await client.download_media(part, file=bytes)
                    sha = hashlib.sha256(data).hexdigest()
                    (folder / f"{sha}.jpg").write_bytes(data)
                    images.append(sha)
                posts.append({"key": origin_key(source.id, parts[0].id), "author": source.name,
                              "url": post_url(source.id, source.username, parts[0].id),
                              "date": parts[0].date.isoformat(), "images": images,
                              "text": next((part.message for part in parts if part.message), "")})
    finally:
        await client.disconnect()
    write_json(folder / "posts.json", posts)
    print("posts", len(posts))


async def run(folder: Path, name: str) -> None:
    labels_path, results_path = set_files(folder, name)
    posts = {post["key"]: post for post in read_json(folder / "posts.json", [])}
    labels = read_json(labels_path, {})
    results = read_json(results_path, {})
    client, market = BrokerClient(), BybitClient()
    symbols = await market.symbols()
    memo = folder.parent / "lessons" / "digest.txt"
    knowledge = memo.read_text(encoding="utf-8") if memo.exists() else ""
    for key in labels:
        if "zones" in results.get(key, {}):
            continue
        post, spooled = posts[key], []
        started = datetime.now(timezone.utc)
        try:
            spooled = [write_spool(SPOOL, (folder / f"{sha}.jpg").read_bytes()) for sha in post["images"]]
            found = await extract_post(client, post["text"], spooled, datetime.fromisoformat(post["date"]), symbols,
                                       market.price_at, live=False, knowledge=knowledge)
        except HermesError as exc:
            results[key] = {"error": exc.code}
            print(key, "error", exc.code)
        else:
            results[key] = {"relevant": found.relevant, "zones": [asdict(zone) for zone in found.zones],
                            "calls": len(found.replies), "tokens": sum(r.tokens.get("total", 0) for r in found.replies),
                            "seconds": (datetime.now(timezone.utc) - started).total_seconds()}
            print(key, "zones", len(found.zones), "seconds", round(results[key]["seconds"]))
        finally:
            for sha in spooled:
                (SPOOL / f"{sha}.jpg").unlink(missing_ok=True)
        write_json(results_path, results)


def _short(zone: dict) -> str:
    low, high = zone.get("zone_low", zone.get("low")), zone.get("zone_high", zone.get("high"))
    flags = ",".join(zone.get("flags", []))
    trigger = "при пробое" if zone.get("trigger") == "break" else ""
    return f"{zone['symbol']} {zone['side']} {low}-{high} {trigger} {flags}".strip()


def show_score(folder: Path, name: str) -> None:
    labels_path, results_path = set_files(folder, name)
    labels, results = read_json(labels_path, {}), read_json(results_path, {})
    done = {key: results[key] for key in labels if "zones" in results.get(key, {})}
    cases = [{"labels": labels[key]["zones"], "predicted": done[key]["zones"]} for key in done]
    sure = [case for case, key in zip(cases, done) if labels[key].get("sure")]
    for key in labels:
        print(key)
        print("  разметка:", "; ".join(_short(zone) for zone in labels[key]["zones"]) or "нет зон")
        print("  модель:  ", "; ".join(_short(zone) for zone in results.get(key, {}).get("zones", []))
              or results.get(key, {}).get("error", "нет зон"))
    print("все:", json.dumps(score(cases), ensure_ascii=False))
    print("уверенная разметка:", json.dumps(score(sure), ensure_ascii=False))
    seconds = sorted(item["seconds"] / item["calls"] for item in done.values())
    if seconds:
        calls = sum(item["calls"] for item in done.values())
        print(f"посты {len(done)}/{len(labels)}, вызовы {calls}, токены {sum(item['tokens'] for item in done.values())}, "
              f"секунд на вызов: среднее {sum(seconds) / len(seconds):.0f}, максимум {seconds[-1]:.0f}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["collect", "run", "score"])
    parser.add_argument("--per-source", type=int, default=12)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--set", dest="name", default="")
    args = parser.parse_args()
    data = Path(os.environ.get("BOT_DATA_DIR", "/var/lib/crypto-signals"))
    folder = data / "golden"
    folder.mkdir(exist_ok=True)
    if args.command == "collect":
        config = Path(os.environ.get("BOT_CONFIG_DIR", "/etc/crypto-signals"))
        asyncio.run(collect(folder, config, args.per_source, args.days))
    elif args.command == "run":
        asyncio.run(run(folder, args.name))
    else:
        show_score(folder, args.name)


if __name__ == "__main__":
    main()
