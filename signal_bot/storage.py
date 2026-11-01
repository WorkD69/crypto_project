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
"""
OPEN = ("WAITING", "ENTERED")
CLOSED = ("SL", "TP", "TIMEOUT")
IDEA_SELECT = ("SELECT ideas.*, posts.author, posts.url, posts.text, posts.edited_text, posts.deleted_at "
               "FROM ideas JOIN posts ON posts.id=post_id ")


class Storage:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)

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

    def _idea_of_post(self, db, post_id: int) -> dict | None:
        row = db.execute(IDEA_SELECT + "WHERE post_id=?", (post_id,)).fetchone()
        return self._idea(row) if row else None

    def record_edit(self, origin_key: str, text: str, at: datetime) -> dict | None:
        """Keep the original text; report only a real change of an idea post."""
        with self._db() as db:
            post = db.execute("SELECT * FROM posts WHERE origin_key=?", (origin_key,)).fetchone()
            if post is None or text == (post["edited_text"] or post["text"]):
                return None
            db.execute("UPDATE posts SET edited_text=?,edited_at=? WHERE id=?", (text, at.isoformat(), post["id"]))
            return self._idea_of_post(db, post["id"])

    def record_delete(self, origin_key: str, at: datetime) -> dict | None:
        with self._db() as db:
            post = db.execute("SELECT id FROM posts WHERE origin_key=?", (origin_key,)).fetchone()
            if post is None:
                return None
            db.execute("UPDATE posts SET deleted_at=? WHERE id=?", (at.isoformat(), post["id"]))
            return self._idea_of_post(db, post["id"])

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
