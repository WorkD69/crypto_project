"""Append-only idea snapshots and event-based paper tracking; no exchange orders."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from scripts.mvp_scenario import Candle


TERMINAL = {"TP", "SL", "CANCELLED", "EXPIRED", "COMPLETED", "AMBIGUOUS"}


def _dt(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Timezone required")
    return result


class IdeaJournal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS ideas (
                    id TEXT PRIMARY KEY, snapshot_json TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, idea_id TEXT NOT NULL,
                    at TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
                    FOREIGN KEY (idea_id) REFERENCES ideas(id));
                CREATE UNIQUE INDEX IF NOT EXISTS event_once
                    ON events(idea_id, kind, at, payload_json);
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def create(self, snapshot: dict) -> str:
        required = {"source", "created_at", "symbol", "timeframe", "direction",
                    "idea_type", "factors", "entry_zone", "sl", "targets", "rr",
                    "valid_until", "rule_version", "chart_path"}
        if not required <= snapshot.keys() or snapshot["idea_type"] not in {"observation", "scenario"}:
            raise ValueError("Incomplete idea snapshot")
        if snapshot["direction"] not in {"LONG", "SHORT", "NEUTRAL"}:
            raise ValueError("Invalid direction")
        if _dt(snapshot["valid_until"]) <= _dt(snapshot["created_at"]):
            raise ValueError("Expiry must follow creation")
        zone = snapshot["entry_zone"]
        if zone is not None and (len(zone) != 2 or zone[0] > zone[1] or zone[0] <= 0):
            raise ValueError("Invalid entry zone")
        payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        idea_id = uuid.uuid4().hex
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self._db() as db:
            db.execute("INSERT INTO ideas VALUES(?,?,?,?)",
                       (idea_id, payload, digest, snapshot["created_at"]))
        return idea_id

    def events(self, idea_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT at,kind,payload_json FROM events WHERE idea_id=? ORDER BY id",
                              (idea_id,)).fetchall()
        return [{"at": row["at"], "kind": row["kind"],
                 **json.loads(row["payload_json"])} for row in rows]

    def get(self, idea_id: str) -> dict:
        with self._db() as db:
            row = db.execute("SELECT * FROM ideas WHERE id=?", (idea_id,)).fetchone()
        if row is None:
            raise KeyError(idea_id)
        snapshot = json.loads(row["snapshot_json"])
        if hashlib.sha256(row["snapshot_json"].encode()).hexdigest() != row["snapshot_sha256"]:
            raise RuntimeError("Idea snapshot hash mismatch")
        events = self.events(idea_id)
        state_event = next((e for e in reversed(events) if e["kind"] not in
                            {"CHECKED", "PUBLISHED", "CHART_PINNED"}), None)
        status = state_event["kind"] if state_event else "WAITING"
        result_r = state_event.get("result_r") if state_event else None
        post = next((e for e in reversed(events) if e["kind"] == "PUBLISHED"), None)
        pinned = next((e for e in reversed(events) if e["kind"] == "CHART_PINNED"), None)
        return {"id": idea_id, "snapshot": snapshot, "status": status,
                "result_r": result_r, "telegram_message_id": post.get("message_id") if post else None,
                "telegram_url": post.get("url") if post else None,
                "chart_path": pinned.get("path") if pinned else snapshot["chart_path"],
                "events": events}

    def add_event(self, idea_id: str, kind: str, *, at: datetime, **payload) -> None:
        current = self.get(idea_id)
        if current["status"] in TERMINAL:
            raise ValueError("Idea already terminal")
        if at.tzinfo is None:
            raise ValueError("Timezone required")
        if at < _dt(current["snapshot"]["created_at"]):
            raise ValueError("Event predates idea")
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO events(idea_id,at,kind,payload_json) VALUES(?,?,?,?)",
                       (idea_id, at.isoformat(), kind, body))

    def assume_fill(self, idea_id: str, *, at: datetime, price: float) -> None:
        record = self.get(idea_id)
        zone = record["snapshot"]["entry_zone"]
        if record["snapshot"]["idea_type"] != "scenario" or zone is None or not zone[0] <= price <= zone[1]:
            raise ValueError("Assumed fill requires a scenario and price inside entry zone")
        if record["snapshot"]["sl"] is None or not record["snapshot"]["targets"]:
            raise ValueError("Risk levels required")
        if at >= _dt(record["snapshot"]["valid_until"]):
            raise ValueError("Assumed fill after expiry")
        self.add_event(idea_id, "ASSUMED_FILL", at=at, price=price)

    def complete(self, idea_id: str, *, at: datetime, exit_price: float) -> None:
        record = self.get(idea_id)
        fill = next((e for e in reversed(record["events"])
                     if e["kind"] == "ASSUMED_FILL"), None)
        if fill is None or at <= _dt(fill["at"]):
            raise ValueError("Completion requires an earlier assumed fill")
        sl = record["snapshot"]["sl"]
        risk = abs(fill["price"] - sl)
        if risk <= 0:
            raise ValueError("Invalid risk")
        sign = 1 if record["snapshot"]["direction"] == "LONG" else -1
        result_r = round(sign * (exit_price - fill["price"]) / risk, 4)
        self.add_event(idea_id, "COMPLETED", at=at, exit_price=exit_price,
                       result_r=result_r)

    def check_candles(self, idea_id: str, candles: list[Candle], *, as_of: datetime,
                      interval: timedelta) -> dict:
        record = self.get(idea_id)
        snapshot = record["snapshot"]
        created = _dt(snapshot["created_at"])
        expiry = _dt(snapshot["valid_until"])
        if as_of.tzinfo is None or interval <= timedelta(0):
            raise ValueError("Invalid check time or interval")
        if record["status"] in TERMINAL:
            return record
        last_check = max((e["at"] for e in record["events"] if e["kind"] == "CHECKED"), default=None)
        since = _dt(last_check) if last_check else created
        relevant = sorted((c for c in candles if c.start >= since and c.start >= created
                           and c.start + interval <= as_of and c.start < expiry),
                          key=lambda c: c.start)
        fill = next((e for e in reversed(record["events"])
                     if e["kind"] == "ASSUMED_FILL"), None)
        for candle in relevant:
            if fill and candle.start > _dt(fill["at"]):
                sl = snapshot["sl"]
                tp = snapshot["targets"][0]
                if snapshot["direction"] == "LONG":
                    hit_sl, hit_tp = candle.low <= sl, candle.high >= tp
                    risk = fill["price"] - sl
                    reward = tp - fill["price"]
                else:
                    hit_sl, hit_tp = candle.high >= sl, candle.low <= tp
                    risk = sl - fill["price"]
                    reward = fill["price"] - tp
                if risk <= 0:
                    raise ValueError("Invalid assumed-fill risk")
                if hit_sl or hit_tp:
                    kind = "AMBIGUOUS" if hit_sl and hit_tp else "SL" if hit_sl else "TP"
                    result_r = None if kind == "AMBIGUOUS" else -1.0 if kind == "SL" else round(reward/risk, 4)
                    self.add_event(idea_id, kind, at=candle.start + interval,
                                   result_r=result_r, candle_start=candle.start.isoformat())
                    return self.get(idea_id)
            elif not fill and snapshot["entry_zone"] is not None:
                low, high = snapshot["entry_zone"]
                if candle.low <= high and candle.high >= low and record["status"] == "WAITING":
                    self.add_event(idea_id, "TOUCHED", at=candle.start + interval,
                                   candle_start=candle.start.isoformat())
                    record = self.get(idea_id)
        if relevant:
            self.add_event(idea_id, "CHECKED", at=relevant[-1].start + interval)
        if as_of >= expiry and self.get(idea_id)["status"] not in TERMINAL:
            self.add_event(idea_id, "EXPIRED", at=expiry)
        return self.get(idea_id)
