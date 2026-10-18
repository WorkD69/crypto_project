"""Inspect and advance paper ideas. No Bybit keys and no order endpoints."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.idea_journal import IdeaJournal
from scripts.run_research import DB, bybit, candles_from_response


INTERVALS = {"1H": ("60", timedelta(hours=1)),
             "4H": ("240", timedelta(hours=4)),
             "1D": ("D", timedelta(days=1))}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("snapshot_json", type=Path)
    for name in ("status", "check", "cancel", "assume-fill", "complete", "publish"):
        sub = commands.add_parser(name)
        sub.add_argument("idea_id")
        if name == "assume-fill":
            sub.add_argument("price", type=float)
        if name == "complete":
            sub.add_argument("exit_price", type=float)
        if name == "publish":
            sub.add_argument("message_id")
            sub.add_argument("--url")
    args = parser.parse_args()
    journal = IdeaJournal(args.db)
    if args.command == "create":
        idea_id = journal.create(json.loads(args.snapshot_json.read_text(encoding="utf-8")))
        record = journal.get(idea_id)
    else:
        record = journal.get(args.idea_id)
        now = datetime.now(timezone.utc)
        if args.command == "check":
            snapshot = record["snapshot"]
            if snapshot["timeframe"] not in INTERVALS:
                raise ValueError("Unsupported timeframe")
            code, duration = INTERVALS[snapshot["timeframe"]]
            created = datetime.fromisoformat(snapshot["created_at"])
            valid_until = datetime.fromisoformat(snapshot["valid_until"])
            end = min(now, valid_until) + duration
            if now < created:
                raise ValueError("Idea is from the future")
            if record["status"] not in {"TP", "SL", "CANCELLED", "EXPIRED", "COMPLETED", "AMBIGUOUS"}:
                data = bybit("kline", category="linear", symbol=snapshot["symbol"],
                             interval=code, start=int(created.timestamp() * 1000),
                             end=int(end.timestamp() * 1000), limit=1000)
                candles = candles_from_response(data, as_of=now, interval=duration)
                record = journal.check_candles(args.idea_id, candles, as_of=now, interval=duration)
        elif args.command == "cancel":
            journal.add_event(args.idea_id, "CANCELLED", at=now, reason="manual")
            record = journal.get(args.idea_id)
        elif args.command == "assume-fill":
            journal.assume_fill(args.idea_id, at=now, price=args.price)
            record = journal.get(args.idea_id)
        elif args.command == "complete":
            journal.complete(args.idea_id, at=now, exit_price=args.exit_price)
            record = journal.get(args.idea_id)
        elif args.command == "publish":
            journal.add_event(args.idea_id, "PUBLISHED", at=now,
                              message_id=args.message_id, url=args.url)
            record = journal.get(args.idea_id)
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
