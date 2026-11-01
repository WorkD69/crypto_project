"""SQLite state of the bot: owner, posts, ideas and their tracked outcome."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from signal_bot.tracker import Trade

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY, author TEXT NOT NULL, origin_key TEXT UNIQUE, url TEXT,
    text TEXT NOT NULL, kind TEXT NOT NULL, received_at TEXT NOT NULL,
    edited_text TEXT, edited_at TEXT, deleted_at TEXT);
CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL REFERENCES posts(id), symbol TEXT NOT NULL,
    side TEXT, entry_low REAL, entry_high REAL, stop REAL, targets TEXT NOT NULL,
    entry_assumed INTEGER NOT NULL, price REAL, warnings TEXT NOT NULL, created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'WAITING', decision TEXT, card_message_id INTEGER,
    fill_price REAL, exit_price REAL, result_r REAL, ambiguous INTEGER NOT NULL DEFAULT 0, last_bar TEXT);
CREATE TABLE IF NOT EXISTS post_parts (
    part_key TEXT PRIMARY KEY, post_id INTEGER NOT NULL REFERENCES posts(id), message_id INTEGER,
    image_sha TEXT, file_id TEXT);
CREATE TABLE IF NOT EXISTS zones (
    id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL REFERENCES posts(id), image TEXT, symbol TEXT NOT NULL,
    side TEXT, zone_low REAL, zone_high REAL, stop REAL, targets TEXT NOT NULL, timeframe TEXT,
    trigger TEXT NOT NULL, confidence REAL NOT NULL, evidence TEXT NOT NULL, chart_price REAL, price REAL,
    flags TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'WATCHING',
    decision TEXT, card_message_id INTEGER, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS zone_events (
    id INTEGER PRIMARY KEY, zone_id INTEGER NOT NULL REFERENCES zones(id), post_id INTEGER REFERENCES posts(id),
    kind TEXT NOT NULL, version INTEGER NOT NULL, before TEXT, note TEXT, created_at TEXT NOT NULL, notified_at TEXT);
"""
POST_COLUMNS = {"album": "TEXT", "reply_to": "INTEGER", "posted_at": "TEXT", "ready_at": "TEXT",
                "attempts": "INTEGER NOT NULL DEFAULT 0", "last_error": "TEXT", "notice_id": "INTEGER"}
ZONE_FIELDS = ("symbol", "side", "zone_low", "zone_high", "stop", "targets", "timeframe", "trigger", "confidence",
               "evidence", "chart_price", "price", "flags", "image")
LEVELS = ("symbol", "side", "zone_low", "zone_high", "stop", "targets")
CLOSES = {"cancel": "CANCELLED", "result": "RESULT"}
OPEN_ZONES = ("WATCHING", "ARRIVED")
OWNER_FIX = "OWNER_FIX"
OPEN = ("WAITING", "ENTERED")
CLOSED = ("SL", "TP", "TIMEOUT")
IDEA_SELECT = ("SELECT ideas.*, posts.author, posts.url, posts.text, posts.edited_text, posts.deleted_at "
               "FROM ideas JOIN posts ON posts.id=post_id ")
ZONE_SELECT = ("SELECT zones.*, posts.author, posts.url, posts.text, posts.edited_text, posts.deleted_at, "
               "posts.reply_to, posts.notice_id FROM zones JOIN posts ON posts.id=post_id ")


class Storage:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)
            present = {row["name"] for row in db.execute("PRAGMA table_info(posts)")}
            for name, kind in POST_COLUMNS.items():
                if name not in present:
                    db.execute(f"ALTER TABLE posts ADD COLUMN {name} {kind}")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def owner_id(self) -> int | None:
        with self._db() as db:
            row = db.execute("SELECT value FROM meta WHERE key='owner_id'").fetchone()
        return int(row["value"]) if row else None

    def claim_owner(self, user_id: int) -> bool:
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO meta VALUES('owner_id', ?)", (str(user_id),))
        return self.owner_id() == user_id

    def add_post(self, author, origin_key, url, text, kind, received_at: datetime) -> int | None:
        with self._db() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO posts(author,origin_key,url,text,kind,received_at) VALUES(?,?,?,?,?,?)",
                (author, origin_key, url, text, kind, received_at.isoformat()))
            return cursor.lastrowid if cursor.rowcount else None

    def has_part(self, key: str) -> bool:
        with self._db() as db:
            return db.execute("SELECT 1 FROM post_parts WHERE part_key=? UNION ALL "
                              "SELECT 1 FROM posts WHERE origin_key=?", (key, key)).fetchone() is not None

    def add_part(self, author, origin_key, url, text, posted_at: datetime, album, part_key, message_id, image, file_id,
                 reply_to, ready_at: datetime, received_at: datetime) -> tuple[int, bool] | None:
        """Store a post, or one part of an album, durably before anything can go wrong with it."""
        with self._db() as db:
            if db.execute("SELECT 1 FROM post_parts WHERE part_key=?", (part_key,)).fetchone():
                return None
            found = None
            if album:
                found = db.execute("SELECT id, text FROM posts WHERE album=? AND kind='pending'", (album,)).fetchone()
            if found:
                post_id, created = found["id"], False
                joined = "\n".join(piece for piece in (found["text"], text) if piece)
                db.execute("UPDATE posts SET text=?, ready_at=? WHERE id=?", (joined, ready_at.isoformat(), post_id))
            else:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO posts(author,origin_key,url,text,kind,received_at,album,reply_to,posted_at,"
                    "ready_at) VALUES(?,?,?,?,'pending',?,?,?,?,?)",
                    (author, origin_key, url, text, received_at.isoformat(), album, reply_to, posted_at.isoformat(),
                     ready_at.isoformat()))
                if not cursor.rowcount:
                    return None
                post_id, created = cursor.lastrowid, True
            db.execute("INSERT INTO post_parts VALUES(?,?,?,?,?)", (part_key, post_id, message_id, image, file_id))
        return post_id, created

    def next_post(self, now: datetime) -> dict | None:
        """Fresh posts first: a retried backlog must not hold up a live post."""
        with self._db() as db:
            row = db.execute("SELECT * FROM posts WHERE kind='pending' AND ready_at<=? ORDER BY attempts>0, id LIMIT 1",
                             (now.isoformat(),)).fetchone()
        return dict(row) if row else None

    def get_post(self, post_id: int) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        return dict(row) if row else None

    def post_parts(self, post_id: int) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM post_parts WHERE post_id=? ORDER BY message_id", (post_id,)).fetchall()
        return [dict(row) for row in rows]

    def post_images(self, post_id: int) -> list[str]:
        return [part["image_sha"] for part in self.post_parts(post_id) if part["image_sha"]]

    def waiting_images(self) -> set[str]:
        with self._db() as db:
            rows = db.execute("SELECT DISTINCT image_sha FROM post_parts JOIN posts ON posts.id=post_id "
                              "WHERE kind='pending' AND image_sha IS NOT NULL").fetchall()
        return {row["image_sha"] for row in rows}

    def count_unparsed(self) -> int:
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM posts WHERE kind='pending'").fetchone()[0]

    def postpone_post(self, post_id: int, retry_at: datetime, error: str) -> None:
        with self._db() as db:
            db.execute("UPDATE posts SET ready_at=?, attempts=attempts+1, last_error=? WHERE id=?",
                       (retry_at.isoformat(), error, post_id))

    def give_up_post(self, post_id: int, error: str) -> None:
        with self._db() as db:
            db.execute("UPDATE posts SET kind='failed', last_error=? WHERE id=?", (error, post_id))

    def set_notice(self, post_id: int, message_id: int) -> None:
        with self._db() as db:
            db.execute("UPDATE posts SET notice_id=? WHERE id=?", (message_id, post_id))

    @staticmethod
    def _event(db, zone_id, post_id, kind, version, before, note, at: datetime, told: bool) -> None:
        db.execute("INSERT INTO zone_events(zone_id,post_id,kind,version,before,note,created_at,notified_at) "
                   "VALUES(?,?,?,?,?,?,?,?)",
                   (zone_id, post_id, kind, version, json.dumps(before) if before else None, note, at.isoformat(),
                    at.isoformat() if told else None))

    @staticmethod
    def _levels(row) -> dict:
        return {name: json.loads(row[name]) if name == "targets" else row[name] for name in LEVELS}

    def _new_zone(self, db, post_id: int, fields: dict, at: datetime) -> None:
        values = [json.dumps(fields[name]) if name in ("targets", "flags") else fields[name] for name in ZONE_FIELDS]
        zone_id = db.execute(f"INSERT INTO zones(post_id,{','.join(ZONE_FIELDS)},created_at) "
                             f"VALUES(?,{','.join('?' * len(ZONE_FIELDS))},?)",
                             (post_id, *values, at.isoformat())).lastrowid
        self._event(db, zone_id, post_id, "new", 1, None, None, at, True)

    def _update_zone(self, db, post_id: int, zone_id: int, fields: dict, at: datetime) -> None:
        """The author may repeat only what changed, so a missing level keeps its old value."""
        row = db.execute("SELECT * FROM zones WHERE id=?", (zone_id,)).fetchone()
        low, high = fields.get("zone_low"), fields.get("zone_high")
        db.execute("UPDATE zones SET zone_low=?, zone_high=?, stop=?, targets=?, flags=?, version=version+1 WHERE id=?",
                   (row["zone_low"] if low is None else low, row["zone_high"] if high is None else high,
                    row["stop"] if fields.get("stop") is None else fields["stop"],
                    json.dumps(fields["targets"]) if fields.get("targets") else row["targets"],
                    json.dumps(fields.get("flags", [])), zone_id))
        self._event(db, zone_id, post_id, "update", row["version"] + 1, self._levels(row), None, at, False)

    def _close_zone(self, db, post_id: int, zone_id: int, action: str, at: datetime) -> None:
        row = db.execute("SELECT * FROM zones WHERE id=?", (zone_id,)).fetchone()
        db.execute("UPDATE zones SET status=? WHERE id=?", (CLOSES[action], zone_id))
        self._event(db, zone_id, post_id, action, row["version"], None, None, at, False)

    def save_parse(self, post_id: int, kind: str, items: list[tuple], at: datetime) -> None:
        """All of a post's reading lands at once, so a crash cannot make its zones appear twice."""
        with self._db() as db:
            for action, fields, zone_id in items:
                if action == "new":
                    self._new_zone(db, post_id, fields, at)
                elif action == "update":
                    self._update_zone(db, post_id, zone_id, fields, at)
                else:
                    self._close_zone(db, post_id, zone_id, action, at)
            db.execute("UPDATE posts SET kind=? WHERE id=?", (kind, post_id))

    @staticmethod
    def _zone(row) -> dict:
        zone = dict(row)
        zone["targets"] = json.loads(zone["targets"])
        zone["flags"] = json.loads(zone["flags"])
        return zone

    def _zones(self, where: str, *values) -> list[dict]:
        with self._db() as db:
            rows = db.execute(ZONE_SELECT + where + " ORDER BY zones.id", values).fetchall()
        return [self._zone(row) for row in rows]

    def get_zone(self, zone_id: int) -> dict | None:
        found = self._zones("WHERE zones.id=?", zone_id)
        return found[0] if found else None

    def zones_of_post(self, post_id: int) -> list[dict]:
        return self._zones("WHERE post_id=?", post_id)

    def zones_since(self, since: datetime) -> list[dict]:
        return self._zones("WHERE created_at>=?", since.isoformat())

    def open_zones(self, author: str | None = None) -> list[dict]:
        if author is None:
            return self._zones(f"WHERE status IN {OPEN_ZONES}")
        return self._zones(f"WHERE status IN {OPEN_ZONES} AND author=?", author)

    def unsent_zones(self) -> list[dict]:
        return self._zones("WHERE card_message_id IS NULL AND status='WATCHING'")

    def set_zone_card(self, zone_id: int, message_id: int) -> None:
        with self._db() as db:
            db.execute("UPDATE zones SET card_message_id=? WHERE id=?", (message_id, zone_id))

    def set_zone_decision(self, zone_id: int, decision: str) -> bool:
        with self._db() as db:
            return db.execute("UPDATE zones SET decision=? WHERE id=? AND decision IS NULL",
                              (decision, zone_id)).rowcount == 1

    @staticmethod
    def _zone_event(row) -> dict:
        event = dict(row)
        event["before"] = json.loads(event["before"]) if event["before"] else None
        return event

    def zone_events(self, zone_id: int) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM zone_events WHERE zone_id=? ORDER BY id", (zone_id,)).fetchall()
        return [self._zone_event(row) for row in rows]

    def unsent_events(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM zone_events WHERE notified_at IS NULL ORDER BY id").fetchall()
        return [self._zone_event(row) for row in rows]

    def event_sent(self, event_id: int, at: datetime) -> None:
        with self._db() as db:
            db.execute("UPDATE zone_events SET notified_at=? WHERE id=?", (at.isoformat(), event_id))

    def fix_zone(self, zone_id: int, fields: dict, note: str, at: datetime) -> None:
        with self._db() as db:
            row = db.execute("SELECT * FROM zones WHERE id=?", (zone_id,)).fetchone()
            names = [name for name in LEVELS if name in fields]
            values = [json.dumps(fields[name]) if name == "targets" else fields[name] for name in names]
            changes = "".join(f"{name}=?, " for name in names)
            db.execute(f"UPDATE zones SET {changes}flags=?, version=version+1 WHERE id=?",
                       (*values, json.dumps([OWNER_FIX]), zone_id))
            self._event(db, zone_id, row["post_id"], "fix", row["version"] + 1, self._levels(row), note, at, True)

    def reject_zone(self, zone_id: int, note: str, at: datetime) -> None:
        with self._db() as db:
            row = db.execute("SELECT * FROM zones WHERE id=?", (zone_id,)).fetchone()
            db.execute("UPDATE zones SET status='REJECTED' WHERE id=?", (zone_id,))
            self._event(db, zone_id, row["post_id"], "reject", row["version"], self._levels(row), note, at, True)

    def waiting_fix(self) -> int | None:
        with self._db() as db:
            row = db.execute("SELECT value FROM meta WHERE key='waiting_fix'").fetchone()
        return int(row["value"]) if row else None

    def set_waiting_fix(self, zone_id: int | None) -> None:
        with self._db() as db:
            db.execute("DELETE FROM meta WHERE key='waiting_fix'")
            if zone_id is not None:
                db.execute("INSERT INTO meta VALUES('waiting_fix', ?)", (str(zone_id),))

    def posts_since(self, since: datetime, kind: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM posts WHERE received_at>=? AND kind=? ORDER BY received_at",
                              (since.isoformat(), kind)).fetchall()
        return [dict(row) for row in rows]

    def add_idea(self, post_id, symbol, side, entry_low, entry_high, stop, targets, entry_assumed,
                 price, warnings, created_at: datetime, status: str = "WAITING") -> int:
        with self._db() as db:
            return db.execute(
                "INSERT INTO ideas(post_id,symbol,side,entry_low,entry_high,stop,targets,entry_assumed,"
                "price,warnings,created_at,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (post_id, symbol, side, entry_low, entry_high, stop, json.dumps(targets), int(entry_assumed),
                 price, json.dumps(warnings), created_at.isoformat(), status)).lastrowid

    @staticmethod
    def _idea(row) -> dict:
        idea = dict(row)
        idea["targets"] = json.loads(idea["targets"])
        idea["warnings"] = json.loads(idea["warnings"])
        return idea

    def get_idea(self, idea_id: int) -> dict | None:
        with self._db() as db:
            row = db.execute(IDEA_SELECT + "WHERE ideas.id=?", (idea_id,)).fetchone()
        return self._idea(row) if row else None

    def open_ideas(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute(IDEA_SELECT + f"WHERE status IN {OPEN} ORDER BY ideas.id").fetchall()
        return [self._idea(row) for row in rows]

    def ideas_since(self, since: datetime) -> list[dict]:
        with self._db() as db:
            rows = db.execute(IDEA_SELECT + "WHERE created_at>=? ORDER BY ideas.id",
                              (since.isoformat(),)).fetchall()
        return [self._idea(row) for row in rows]

    def _post_cards(self, post_id: int) -> dict | None:
        """What the owner was told about a post, for reporting its edit or removal."""
        with self._db() as db:
            row = db.execute(IDEA_SELECT + "WHERE post_id=?", (post_id,)).fetchone()
        idea, zones = self._idea(row) if row else None, self.zones_of_post(post_id)
        return {"idea": idea, "zones": zones} if idea or zones else None

    def record_edit(self, origin_key: str, text: str, at: datetime) -> dict | None:
        """Keep the original text; report only a real change of a post with cards."""
        with self._db() as db:
            post = db.execute("SELECT * FROM posts WHERE origin_key=?", (origin_key,)).fetchone()
            if post is None or text == (post["edited_text"] or post["text"]):
                return None
            db.execute("UPDATE posts SET edited_text=?,edited_at=? WHERE id=?", (text, at.isoformat(), post["id"]))
        return self._post_cards(post["id"])

    def record_delete(self, origin_key: str, at: datetime) -> dict | None:
        with self._db() as db:
            post = db.execute("SELECT id FROM posts WHERE origin_key=?", (origin_key,)).fetchone()
            if post is None:
                return None
            db.execute("UPDATE posts SET deleted_at=? WHERE id=?", (at.isoformat(), post["id"]))
        return self._post_cards(post["id"])

    def cursor(self, source_id: int) -> int | None:
        with self._db() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (f"cursor:{source_id}",)).fetchone()
        return int(row["value"]) if row else None

    def set_cursor(self, source_id: int, message_id: int) -> None:
        current = self.cursor(source_id)
        if current is None or message_id > current:
            with self._db() as db:
                db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"cursor:{source_id}", str(message_id)))

    def set_decision(self, idea_id: int, decision: str) -> bool:
        with self._db() as db:
            return db.execute("UPDATE ideas SET decision=? WHERE id=? AND decision IS NULL",
                              (decision, idea_id)).rowcount == 1

    def set_card_message(self, idea_id: int, message_id: int) -> None:
        with self._db() as db:
            db.execute("UPDATE ideas SET card_message_id=? WHERE id=?", (message_id, idea_id))

    @staticmethod
    def trade(idea: dict) -> Trade:
        last_bar = idea["last_bar"]
        return Trade(side=idea["side"], entry_low=idea["entry_low"], entry_high=idea["entry_high"],
                     stop=idea["stop"], target=idea["targets"][0] if idea["targets"] else None,
                     created_at=datetime.fromisoformat(idea["created_at"]), status=idea["status"],
                     fill_price=idea["fill_price"], exit_price=idea["exit_price"], result_r=idea["result_r"],
                     ambiguous=bool(idea["ambiguous"]),
                     last_bar=datetime.fromisoformat(last_bar) if last_bar else None)

    def save_trade(self, idea_id: int, trade: Trade) -> None:
        with self._db() as db:
            db.execute("UPDATE ideas SET status=?,fill_price=?,exit_price=?,result_r=?,ambiguous=?,last_bar=? "
                       "WHERE id=?", (trade.status, trade.fill_price, trade.exit_price, trade.result_r,
                                      int(trade.ambiguous), trade.last_bar.isoformat() if trade.last_bar else None,
                                      idea_id))

    def stats(self) -> list[dict]:
        closed = ",".join(f"'{status}'" for status in CLOSED)
        with self._db() as db:
            rows = db.execute(f"""
                SELECT author, COUNT(*) AS ideas,
                       COALESCE(SUM(status IN ({closed})), 0) AS closed,
                       COALESCE(SUM(status IN ({closed}) AND result_r > 0), 0) AS wins,
                       COALESCE(SUM(CASE WHEN status IN ({closed}) THEN result_r END), 0) AS total_r,
                       COALESCE(SUM(CASE WHEN status IN ({closed}) AND decision='take' THEN result_r END), 0) AS taken_r,
                       COALESCE(SUM(deleted_at IS NOT NULL), 0) AS deleted
                FROM ideas JOIN posts ON posts.id=post_id GROUP BY author ORDER BY total_r DESC""").fetchall()
        return [dict(row) for row in rows]
