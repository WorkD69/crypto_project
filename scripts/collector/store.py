"""Collector-owned SQLite, separate from the immutable trading idea journal."""
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def normalize_message(source_key, raw):
    message_id = raw.get("message_id")
    if message_id is not None and not re.fullmatch(r"[1-9][0-9]*", str(message_id)):
        message_id = None
    timestamp = raw.get("published_at")
    if timestamp:
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            timestamp = parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
        except ValueError:
            timestamp = None
    media = raw.get("media", [])
    observed = {"text": raw.get("text", ""), "album_id": raw.get("album_id"),
                "media": [{k: m.get(k) for k in ("position", "kind", "label", "observed_id")}
                          for m in media]}
    return {"schema_version": "collector.material.v1", "source_key": source_key,
            "source_identity": raw.get("source_identity"),
            "message_id": str(message_id) if message_id else None,
            "identity_basis": "telegram_web_visible_dom" if message_id else "UNAVAILABLE",
            "published_at": timestamp, "original_time_raw": raw.get("original_time_raw"),
            "received_at": utc_now(), "url": raw.get("url"),
            "content_type": "mixed" if media and raw.get("text") else "media" if media else "text",
            "text": observed["text"], "album_id": observed["album_id"], "media": media,
            "content_sha256": digest(observed),
            "processing_state": "COLLECTED" if timestamp else "NEEDS_REVIEW",
            "warnings": raw.get("warnings", []) + ([] if timestamp else ["ORIGINAL_TIME_UNAVAILABLE"])}


class CollectorStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    source TEXT, message_id TEXT, version INTEGER, hash TEXT, payload TEXT,
                    PRIMARY KEY(source, message_id, version));
                CREATE TABLE IF NOT EXISTS cursors (
                    source TEXT PRIMARY KEY, high_water_id TEXT, coverage TEXT,
                    scanned INTEGER, checked_at TEXT, detail TEXT);
                CREATE TABLE IF NOT EXISTS quarantine (
                    hash TEXT PRIMARY KEY, payload TEXT, received_at TEXT);
                CREATE TABLE IF NOT EXISTS media (
                    source TEXT, message_id TEXT, version INTEGER, position INTEGER, payload TEXT,
                    PRIMARY KEY(source, message_id, version, position));
                CREATE TABLE IF NOT EXISTS media_variants (
                    source TEXT, message_id TEXT, version INTEGER, position INTEGER,
                    sha256 TEXT, payload TEXT, observed_at TEXT,
                    PRIMARY KEY(source, message_id, version, position, sha256));
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, at TEXT, command TEXT, state TEXT, detail TEXT);
                CREATE TABLE IF NOT EXISTS observations (
                    source TEXT, message_id TEXT, version INTEGER, received_at TEXT,
                    time_raw TEXT, published_at TEXT,
                    UNIQUE(source,message_id,version,time_raw,published_at));
                CREATE TRIGGER IF NOT EXISTS immutable_message_update
                    BEFORE UPDATE ON messages BEGIN SELECT RAISE(ABORT, 'immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_message_delete
                    BEFORE DELETE ON messages BEGIN SELECT RAISE(ABORT, 'immutable'); END;
            """)
            from scripts.collector.events import SCHEMA
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, material):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if material["message_id"] is None:
                quarantine_key = digest({k: material[k] for k in
                    ("source_key", "source_identity", "content_sha256", "original_time_raw")})
                db.execute("INSERT OR IGNORE INTO quarantine VALUES (?,?,?)",
                           (quarantine_key, canonical(material), utc_now()))
                return {"action": "quarantined", "version": None}
            key = material["source_key"], material["message_id"]
            latest = db.execute("SELECT version, hash FROM messages WHERE source=? AND message_id=? "
                                "ORDER BY version DESC LIMIT 1", key).fetchone()
            old_media = {}
            if latest:
                old_media = {r["position"]: json.loads(r["payload"]) for r in db.execute(
                    "SELECT position,payload FROM media WHERE source=? AND message_id=? AND version=?",
                    (*key, latest["version"]))}
            # Telegram Web can return different image resolutions for the same
            # media. Bytes alone are not evidence of an edited Telegram post.
            changed = latest is None or latest["hash"] != material["content_sha256"]
            version = (latest["version"] + 1 if latest else 1) if changed else latest["version"]
            if changed:
                payload = dict(material, version=version)
                db.execute("INSERT INTO messages VALUES (?,?,?,?,?)",
                           (*key, version, material["content_sha256"], canonical(payload)))
            if material.get("published_at"):
                db.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?)",
                           (*key, version, material["received_at"], material.get("original_time_raw"), material["published_at"]))
            for item in material["media"]:
                old = db.execute("SELECT payload FROM media WHERE source=? AND message_id=? "
                                 "AND version=? AND position=?", (*key, version, item["position"])).fetchone()
                if old and item.get("sha256") and (prior := json.loads(old[0])).get("sha256") \
                        and item["sha256"] != prior["sha256"]:
                    db.execute("INSERT OR IGNORE INTO media_variants VALUES (?,?,?,?,?,?,?)",
                               (*key, version, item["position"], item["sha256"], canonical(item), utc_now()))
                    item = prior
                if old and item.get("state") != "DOWNLOADED" and json.loads(old[0]).get("state") == "DOWNLOADED":
                    previous = json.loads(old[0])
                    previous.update(last_attempt_state=item.get("state"),
                                    last_attempt_error=item.get("error"), last_attempt_at=utc_now())
                    item = previous
                db.execute("INSERT OR REPLACE INTO media VALUES (?,?,?,?,?)",
                           (*key, version, item["position"], canonical(item)))
            if changed:
                from scripts.collector.events import insert_event
                insert_event(db, material, version, 'NEW' if latest is None else 'EDITED')
            return {"action": "created" if latest is None else "edited" if changed else "duplicate",
                    "version": version}

    def events(self):
        from scripts.collector.events import list_events
        return list_events(self)

    def claim_event(self, event_key=None, live_budget=False):
        from scripts.collector.events import claim
        return claim(self, event_key, live_budget)

    def recover_events(self, now=None):
        from scripts.collector.events import recover
        return recover(self, now)

    def get(self, source, message_id, version=None):
        with self.connect() as db:
            query = "SELECT payload, version FROM messages WHERE source=? AND message_id=?"
            params = [source, message_id]
            if version is not None:
                query += " AND version=?"
                params.append(version)
            row = db.execute(query + " ORDER BY version DESC LIMIT 1", params).fetchone()
            if not row:
                return None
            item = json.loads(row["payload"])
            observation = db.execute("SELECT * FROM observations WHERE source=? AND message_id=? AND version<=? "
                                     "ORDER BY received_at DESC LIMIT 1", (source, message_id, row["version"])).fetchone()
            if observation:
                item.update(published_at=observation["published_at"], original_time_raw=observation["time_raw"])
                item["warnings"] = [w for w in item["warnings"] if w != "ORIGINAL_TIME_UNAVAILABLE"]
                item["processing_state"] = "COLLECTED" if not item["warnings"] else "NEEDS_REVIEW"
            item["media"] = [json.loads(m[0]) for m in db.execute(
                "SELECT payload FROM media WHERE source=? AND message_id=? AND version=? ORDER BY position",
                (source, message_id, row["version"]))]
            variant = db.execute("SELECT 1 FROM media_variants WHERE source=? AND message_id=? AND version=? LIMIT 1",
                                 (source, message_id, row["version"])).fetchone()
            prior_same_hash = db.execute("SELECT version FROM messages WHERE source=? AND message_id=? "
                                         "AND version<? AND hash=? ORDER BY version DESC LIMIT 1",
                                         (source, message_id, row["version"], item["content_sha256"])).fetchone()
            if prior_same_hash:
                previous = {r["position"]: json.loads(r["payload"]).get("sha256") for r in db.execute(
                    "SELECT position,payload FROM media WHERE source=? AND message_id=? AND version=?",
                    (source, message_id, prior_same_hash["version"]))}
                variant = variant or any(m.get("sha256") and previous.get(m["position"]) and
                                         m["sha256"] != previous[m["position"]] for m in item["media"])
            if variant:
                item["warnings"] = list(dict.fromkeys(item["warnings"] + ["MEDIA_BYTES_CHANGED_UNVERIFIED"]))
                item["processing_state"] = "NEEDS_REVIEW"
            if any(m.get("state") != "DOWNLOADED" or m.get("last_attempt_state", "DOWNLOADED") != "DOWNLOADED" for m in item["media"]):
                item["processing_state"] = "NEEDS_REVIEW"
            return item

    def cursor(self, source):
        with self.connect() as db:
            row = db.execute("SELECT * FROM cursors WHERE source=?", (source,)).fetchone()
            return dict(row) if row else None

    def checkpoint(self, source, high_water_id, coverage, scanned, detail=None):
        previous = self.cursor(source)
        old = previous and previous["high_water_id"]
        high = max((str(x) for x in (old, high_water_id) if x), key=int, default=None)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?,?,?,?,?)",
                       (source, high, coverage, scanned, utc_now(), canonical(detail or {})))

    def record_run(self, command, state, detail):
        with self.connect() as db:
            db.execute("INSERT INTO runs(at,command,state,detail) VALUES (?,?,?,?)",
                       (utc_now(), command, state, canonical(detail)))

    def status(self):
        with self.connect() as db:
            return {"messages": db.execute("SELECT COUNT(*) FROM (SELECT DISTINCT source,message_id FROM messages)").fetchone()[0],
                    "versions": db.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                    "media_variants": db.execute("SELECT COUNT(*) FROM media_variants").fetchone()[0],
                    "quarantined": db.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0],
                    "cursors": [dict(r) for r in db.execute("SELECT * FROM cursors")],
                    "recent_runs": [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 10")]}

    def export(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            keys = db.execute("SELECT source,message_id,version FROM messages").fetchall()
        for source, message_id, version in keys:
            item = self.get(source, message_id, version)
            target = directory / f"{source}-{message_id}-v{version}.json"
            temp = target.with_suffix(".tmp")
            temp.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(target)
