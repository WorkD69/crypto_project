"""Stage 0 invariants: real SQLite, bounded config, no Telegram account needed."""
import json
import tempfile
import unittest
from pathlib import Path

from scripts.collector.config import load_config
from scripts.collector.store import CollectorStore, normalize_message
from scripts.collector.browser import ProfileLock, session_state
from scripts.collector.telegram import coverage_state, parse_ui_time, validate_source_route
from scripts.collector.pipeline import pipeline_input
from scripts.collector_fixture import require_own_fixture


class CollectorStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "collector.sqlite3"
        self.store = CollectorStore(self.db)

    def message(self, **changes):
        raw = dict(message_id="42", text="BTCUSDT 4H: тест, не сигнал", media=[],
                   published_at=None, original_time_raw="16:30", album_id=None)
        raw.update(changes)
        return normalize_message("saved", raw)

    def test_restart_deduplicates_same_content(self):
        first = self.store.save(self.message())
        again = CollectorStore(self.db).save(self.message())
        self.assertEqual(first["version"], 1)
        self.assertEqual(again["action"], "duplicate")
        self.assertEqual(self.store.status()["versions"], 1)

    def test_edit_preserves_original_version_and_reversion_is_a_new_version(self):
        self.store.save(self.message())
        edited = self.store.save(self.message(text="Правка: NEEDS_REVIEW"))
        reverted = self.store.save(self.message())
        self.assertEqual(edited["version"], 2)
        self.assertEqual(reverted["version"], 3)
        self.assertEqual(self.store.get("saved", "42", 1)["text"], "BTCUSDT 4H: тест, не сигнал")

    def test_cursors_are_separate_and_highwater_never_regresses(self):
        self.store.checkpoint("saved", "42", "COMPLETE", 10)
        self.store.checkpoint("other", "8", "PARTIAL", 5)
        self.store.checkpoint("saved", "40", "PARTIAL", 2)
        self.assertEqual(self.store.cursor("saved")["high_water_id"], "42")
        self.assertEqual(self.store.cursor("other")["high_water_id"], "8")

    def test_missing_id_is_quarantined_instead_of_invented(self):
        result = self.store.save(self.message(message_id=None))
        self.assertEqual(result["action"], "quarantined")
        self.assertEqual(self.store.status()["messages"], 0)
        self.assertEqual(self.store.status()["quarantined"], 1)

    def test_idless_quarantine_is_deduplicated_across_receipt_times(self):
        self.store.save(self.message(message_id=None))
        self.store.save(self.message(message_id=None))
        self.assertEqual(self.store.status()["quarantined"], 1)

    def test_missing_original_time_stays_null(self):
        msg = self.message()
        self.assertIsNone(msg["published_at"])
        self.assertEqual(msg["original_time_raw"], "16:30")
        self.assertIsNone(msg["url"])
        self.assertEqual(msg["processing_state"], "NEEDS_REVIEW")

    def test_media_download_status_does_not_create_message_edit(self):
        media = [{"kind": "photo", "position": 0, "state": "PENDING", "path": None}]
        self.store.save(self.message(media=media))
        media[0].update(state="DOWNLOADED", path="abc.jpg", sha256="a" * 64)
        result = self.store.save(self.message(media=media))
        self.assertEqual(result["action"], "duplicate")

    def test_changed_download_bytes_without_visible_edit_are_quarantined_for_review(self):
        media = [{"kind": "photo", "position": 0, "state": "DOWNLOADED", "sha256": "a" * 64}]
        self.store.save(self.message(media=media))
        media[0]["sha256"] = "b" * 64
        result = self.store.save(self.message(media=media))
        self.assertEqual(result["action"], "duplicate")
        self.assertEqual(self.store.status()["versions"], 1)
        self.assertEqual(self.store.get("saved", "42", 1)["media"][0]["sha256"], "a" * 64)
        self.assertEqual(self.store.get("saved", "42")["processing_state"], "NEEDS_REVIEW")
        self.assertIn("MEDIA_BYTES_CHANGED_UNVERIFIED", self.store.get("saved", "42")["warnings"])

    def test_failed_media_recheck_preserves_file_and_records_failure(self):
        media = [{"kind": "photo", "position": 0, "state": "DOWNLOADED", "path": "a.jpg", "sha256": "a" * 64}]
        self.store.save(self.message(media=media))
        self.store.save(self.message(media=[{"kind": "photo", "position": 0, "state": "UNAVAILABLE", "error": "TimeoutError"}]))
        item = self.store.get("saved", "42")
        self.assertEqual(item["media"][0]["path"], "a.jpg")
        self.assertEqual(item["media"][0]["last_attempt_state"], "UNAVAILABLE")
        self.assertEqual(item["processing_state"], "NEEDS_REVIEW")

    def test_later_exact_tooltip_enriches_metadata_without_rewriting_text_version(self):
        self.store.save(self.message())
        msg = self.message(published_at="2026-10-16T15:30:00+00:00", original_time_raw="Oct 16, 2026, 18:30:00")
        self.assertEqual(self.store.save(msg)["action"], "duplicate")
        self.assertEqual(self.store.get("saved", "42")["published_at"], msg["published_at"])
        self.assertEqual(self.store.status()["versions"], 1)

    def test_original_publication_time_carries_to_edited_version(self):
        self.store.save(self.message(published_at="2026-10-16T15:30:00+00:00"))
        self.store.save(self.message(text="Edited text"))
        self.assertEqual(self.store.get("saved", "42")["published_at"], "2026-10-16T15:30:00+00:00")

    def test_outbox_export_is_idempotent_and_includes_provenance(self):
        self.store.save(self.message())
        out = Path(self.tmp.name) / "outbox"
        self.store.export(out)
        self.store.export(out)
        files = list(out.glob("*.json"))
        self.assertEqual(len(files), 1)
        item = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(item["source_key"], "saved")
        self.assertEqual(item["message_id"], "42")
        self.assertEqual(item["version"], 1)
        self.assertIn("content_sha256", item)


class CollectorConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def config(self, data):
        path = Path(self.tmp.name) / "sources.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_rejects_unbounded_history(self):
        with self.assertRaises(ValueError):
            load_config(self.config({"max_messages": 999999, "sources": []}))

    def test_duplicate_source_keys_rejected(self):
        source = dict(key="saved", kind="saved", enabled=True)
        with self.assertRaises(ValueError):
            load_config(self.config({"sources": [source, source]}))

    def test_sample_only_enables_saved_messages(self):
        config = load_config(Path(__file__).resolve().parents[1] / "config" / "collector.sources.example.json")
        enabled = [s["key"] for s in config["sources"] if s.get("enabled")]
        self.assertEqual(enabled, ["saved"])


class BrowserLifecycleTests(unittest.TestCase):
    def test_profile_lock_prevents_overlapping_runs_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProfileLock(Path(directory)):
                with self.assertRaises(RuntimeError):
                    with ProfileLock(Path(directory)):
                        self.fail("Overlapping browser opened")
            with ProfileLock(Path(directory)):
                pass

    def test_login_form_is_not_authenticated(self):
        self.assertEqual(session_state({"login": True, "chat_list": False}), "AUTH_REQUIRED")

    def test_loading_is_not_mislabeled_as_session_loss(self):
        self.assertEqual(session_state({"login": False, "chat_list": False}), "UI_UNRECOGNIZED")

    def test_offline_cached_chat_is_not_treated_as_ready(self):
        self.assertEqual(session_state({"login": False, "chat_list": True, "online": False}), "NETWORK_UNAVAILABLE")

    def test_chat_list_confirms_authenticated_ui(self):
        self.assertEqual(session_state({"login": False, "chat_list": True}), "AUTHENTICATED")


class CoverageTests(unittest.TestCase):
    def test_user_or_bot_with_matching_title_is_not_a_channel(self):
        with self.assertRaises(RuntimeError):
            validate_source_route("channel", "1000000001")
        validate_source_route("channel", "-1000000000001")
    def test_old_anchor_reached_means_bounded_catchup_complete(self):
        self.assertEqual(coverage_state(["44", "43", "42"], "42", False), "COMPLETE")

    def test_gap_is_not_success_when_history_limit_reached(self):
        self.assertEqual(coverage_state(["99", "98"], "42", False), "PARTIAL")

    def test_initial_scan_never_claims_full_history(self):
        self.assertEqual(coverage_state(["99", "98"], None, False), "BOUNDED_INITIAL")

    def test_relative_time_is_not_invented(self):
        self.assertIsNone(parse_ui_time("Mon 12:30"))

    def test_full_ui_tooltip_uses_configured_browser_timezone(self):
        self.assertEqual(parse_ui_time("Oct 12, 2026, 23:39:45"), "2026-10-12T20:39:45+00:00")


class PipelineBoundaryTests(unittest.TestCase):
    def test_fixture_editor_refuses_non_test_message_or_temporary_id(self):
        for mid, text in (("1", "Personal note"), ("1.00001", "CRP-STAGE0 own")):
            with self.assertRaises(ValueError):
                require_own_fixture(mid, text)

    def test_only_own_stage0_material_is_used_by_test_bridge(self):
        item = normalize_message("wyckoff", {"message_id": "1", "text": "some text"})
        with self.assertRaises(ValueError):
            pipeline_input(item, Path("."))

    def test_media_path_cannot_escape_private_runtime(self):
        item = normalize_message("saved", {"message_id": "1", "text": "CRP-STAGE0 test",
            "media": [{"position": 0, "kind": "photo", "state": "DOWNLOADED", "path": "../secret.png"}]})
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                pipeline_input(item, Path(directory))


if __name__ == "__main__":
    unittest.main()
