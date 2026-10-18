"""Bounded, explicitly allowlisted sources. Credentials never belong here."""
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "collector.sources.json"


def runtime_root():
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "CryptoResearchPlatform" / "browser-collector"


def load_config(path=DEFAULT_CONFIG):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    for name, default, low, high in (("max_messages", 200, 1, 200),
                                   ("max_scrolls", 20, 1, 30),
                                   ("overlap_messages", 50, 1, 100),
                                   ("max_media_mb", 128, 1, 256),
                                   ("max_media_per_run", 10, 0, 20)):
        value = data.setdefault(name, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} must be {low}..{high}")
    keys = set()
    for source in data.get("sources", []):
        key = source.get("key", "")
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", key) or key in keys:
            raise ValueError("Invalid or duplicate source key")
        keys.add(key)
        if source.get("kind") not in ("saved", "channel"):
            raise ValueError("Only Saved Messages and allowlisted broadcast channels supported")
        # No arbitrary navigation, account switching, joins or bot interactions.
        username = source.get("username")
        if username and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username):
            raise ValueError("Invalid channel username")
        if source["kind"] == "channel" and not (username or source.get("expected_title")):
            raise ValueError("Channel needs an expected title or username")
    return data
