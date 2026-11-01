import asyncio
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from signal_bot.broker import Broker, Budget, command, neutralize, parse_request, run_process

JPEG = b"\xff\xd8\xff" + b"chart"
SHA = hashlib.sha256(JPEG).hexdigest()
GOOD = json.dumps({"type": "result", "exit_code": 0, "text": "{}"})


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class Runner:
    """Records every command instead of starting docker."""

    def __init__(self, *results):
        self.results, self.calls = list(results), []

    async def __call__(self, argv, stdin, timeout):
        self.calls.append((argv, stdin))
        return self.results.pop(0) if self.results else (0, GOOD.encode(), b"")


def request(prompt="разбери", image=None):
    return json.dumps({"prompt": prompt, "image_sha256": image}).encode()


class CommandTest(unittest.TestCase):
    def test_tools_and_rules_stay_disabled(self):
        argv = command(None)
        for flag in ("--ignore-rules", "--oneshot", "-Q"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--toolsets") + 1], "no_mcp")
        self.assertEqual(argv[argv.index("--max-turns") + 1], "1")
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6-luna")
        self.assertNotIn("--image", argv)

    def test_prompt_goes_through_stdin(self):
        argv = command(None)
        self.assertEqual(argv[argv.index("--query-file") + 1], "-")

    def test_container_enforces_the_timeout(self):
        argv = command(None)
        self.assertLess(argv.index("timeout"), argv.index("/opt/hermes/.venv/bin/hermes"))

    def test_image_path_is_built_from_the_hash_only(self):
        argv = command(SHA)
        self.assertEqual(argv[argv.index("--image") + 1], f"/opt/data/crypto-spool/{SHA}.jpg")


class RequestTest(unittest.TestCase):
    def test_valid_request(self):
        self.assertEqual(parse_request(request("текст", SHA)), ("текст", SHA))

    def test_image_is_optional(self):
        self.assertEqual(parse_request(json.dumps({"prompt": "текст"}).encode()), ("текст", None))

    def test_unknown_fields_are_refused(self):
        raw = json.dumps({"prompt": "текст", "image_sha256": None, "path": "/etc/passwd"}).encode()
        self.assertIsNone(parse_request(raw))

    def test_path_instead_of_hash_is_refused(self):
        self.assertIsNone(parse_request(request("текст", "../../etc/passwd")))

    def test_empty_prompt_is_refused(self):
        self.assertIsNone(parse_request(request("  ")))

    def test_oversized_prompt_is_refused(self):
        self.assertIsNone(parse_request(request("я" * 40_000)))

    def test_broken_json_is_refused(self):
        self.assertIsNone(parse_request(b"{not json"))

    def test_non_object_is_refused(self):
        self.assertIsNone(parse_request(b"[1]"))


class NeutralizeTest(unittest.TestCase):
    def test_context_references_are_broken(self):
        for text in ("см. @file:/opt/data/auth.json", "@url:http://x", "@diff", "@folder:/opt/data"):
            self.assertNotIn(text[text.index("@"):], neutralize(text))

    def test_emails_and_plain_text_stay(self):
        self.assertEqual(neutralize("почта a@b.c, цена 84000"), "почта a@b.c, цена 84000")


class BudgetTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "state.json"
        self.clock = Clock()

    def budget(self, limit=2):
        return Budget(self.path, limit, self.clock)

    def test_calls_stop_at_the_limit(self):
        budget = self.budget()
        self.assertIsNone(budget.refusal())
        budget.spend()
        budget.spend()
        self.assertEqual(budget.refusal(), "DAILY_LIMIT")

    def test_counter_survives_restart(self):
        self.budget().spend()
        self.budget().spend()
        self.assertEqual(self.budget().refusal(), "DAILY_LIMIT")

    def test_counter_resets_next_day(self):
        budget = self.budget()
        budget.spend()
        budget.spend()
        self.clock.now += timedelta(days=1)
        self.assertIsNone(budget.refusal())

    def test_pause_holds_until_its_end(self):
        budget = self.budget()
        budget.pause(timedelta(hours=1))
        self.assertEqual(self.budget().refusal(), "PAUSED")
        self.clock.now += timedelta(hours=1, seconds=1)
        self.assertIsNone(budget.refusal())


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.spool = Path(self.folder.name) / "spool"
        self.spool.mkdir()
        self.clock = Clock()

    def broker(self, *results, limit=5):
        runner = Runner(*results)
        budget = Budget(Path(self.folder.name) / "state.json", limit, self.clock)
        return Broker(self.spool, budget, runner), runner

    def ask(self, broker, raw=None):
        return asyncio.run(broker.handle(raw or request()))

    def test_text_call_returns_the_stream(self):
        broker, runner = self.broker()
        reply = self.ask(broker)
        self.assertEqual(reply, {"ok": True, "stream": GOOD})
        self.assertEqual(runner.calls[0][1], "разбери".encode())

    def test_stdin_prompt_is_neutralized(self):
        broker, runner = self.broker()
        self.ask(broker, request("@file:/opt/data/auth.json"))
        self.assertNotIn(b"@file:", runner.calls[0][1])

    def test_bad_request_never_reaches_hermes(self):
        broker, runner = self.broker()
        self.assertEqual(self.ask(broker, b"junk"), {"ok": False, "error": "BAD_REQUEST"})
        self.assertEqual(runner.calls, [])

    def test_image_is_copied_used_and_removed(self):
        (self.spool / f"{SHA}.jpg").write_bytes(JPEG)
        broker, runner = self.broker()
        reply = self.ask(broker, request(image=SHA))
        self.assertTrue(reply["ok"])
        copy, call, remove = runner.calls
        self.assertIn(f"/opt/data/crypto-spool/{SHA}.jpg", " ".join(copy[0]))
        self.assertEqual(copy[1], JPEG)
        self.assertIn("--image", call[0])
        self.assertIn("rm", remove[0])

    def test_image_is_removed_after_a_failed_call(self):
        (self.spool / f"{SHA}.jpg").write_bytes(JPEG)
        broker, runner = self.broker((0, b"", b""), (1, b"", b"boom"))
        self.assertEqual(self.ask(broker, request(image=SHA))["error"], "HERMES_FAILED")
        self.assertIn("rm", runner.calls[-1][0])

    def test_missing_image_is_refused(self):
        broker, runner = self.broker()
        self.assertEqual(self.ask(broker, request(image=SHA))["error"], "IMAGE_REJECTED")
        self.assertEqual(runner.calls, [])

    def test_image_with_other_content_than_its_hash_is_refused(self):
        (self.spool / f"{SHA}.jpg").write_bytes(JPEG + b"tampered")
        broker, runner = self.broker()
        self.assertEqual(self.ask(broker, request(image=SHA))["error"], "IMAGE_REJECTED")
        self.assertEqual(runner.calls, [])

    def test_non_jpeg_is_refused(self):
        data = b"#!/bin/sh"
        sha = hashlib.sha256(data).hexdigest()
        (self.spool / f"{sha}.jpg").write_bytes(data)
        broker, _ = self.broker()
        self.assertEqual(self.ask(broker, request(image=sha))["error"], "IMAGE_REJECTED")

    def test_failed_image_copy_is_not_sent_to_the_model(self):
        (self.spool / f"{SHA}.jpg").write_bytes(JPEG)
        broker, runner = self.broker((1, b"", b"no space"))
        self.assertEqual(self.ask(broker, request(image=SHA))["error"], "HERMES_FAILED")
        self.assertFalse(any("--image" in call[0] for call in runner.calls))

    def test_timeout_is_reported(self):
        broker, _ = self.broker((124, b"", b""))
        self.assertEqual(self.ask(broker)["error"], "TIMEOUT")

    def test_daily_limit_stops_calls(self):
        broker, runner = self.broker(limit=1)
        self.ask(broker)
        self.assertEqual(self.ask(broker)["error"], "DAILY_LIMIT")
        self.assertEqual(len(runner.calls), 1)

    def test_failed_calls_are_counted(self):
        broker, _ = self.broker((1, b"", b"boom"), limit=1)
        self.ask(broker)
        self.assertEqual(self.ask(broker)["error"], "DAILY_LIMIT")

    def test_limit_signs_pause_the_broker(self):
        failure = json.dumps({"type": "result", "exit_code": 1, "error": "HTTP 429: usage limit reached"}).encode()
        broker, runner = self.broker((1, failure, b""))
        self.assertEqual(self.ask(broker)["error"], "RATE_LIMITED")
        self.assertEqual(self.ask(broker)["error"], "PAUSED")
        self.assertEqual(len(runner.calls), 1)

    def test_limit_words_in_a_good_answer_do_not_pause(self):
        answer = json.dumps({"type": "result", "exit_code": 0, "text": '{"evidence": "limit 429"}'}).encode()
        broker, _ = self.broker((0, answer, b""))
        self.assertTrue(self.ask(broker)["ok"])
        self.assertTrue(self.ask(broker)["ok"])

    def test_limit_words_in_author_text_do_not_pause_on_other_failures(self):
        stream = json.dumps({"type": "text", "text": "usage limit 429"}).encode() + b"\n" + \
            json.dumps({"type": "result", "exit_code": 1, "error": "model gone"}).encode()
        broker, _ = self.broker((1, stream, b""))
        self.assertEqual(self.ask(broker)["error"], "HERMES_FAILED")
        self.assertTrue(self.ask(broker)["ok"])

    def test_calls_run_one_at_a_time(self):
        broker, _ = self.broker()
        active, peak = [0], [0]

        async def slow(argv, stdin, timeout):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            await asyncio.sleep(0)
            active[0] -= 1
            return 0, GOOD.encode(), b""

        broker.run = slow

        async def scenario():
            await asyncio.gather(*(broker.handle(request()) for _ in range(3)))

        asyncio.run(scenario())
        self.assertEqual(peak[0], 1)


class RunProcessTest(unittest.TestCase):
    def test_output_and_exit_code(self):
        code = "import sys; sys.stdout.write(sys.stdin.read()); sys.exit(3)"
        rc, out, _ = asyncio.run(run_process([sys.executable, "-c", code], b"abc", 20))
        self.assertEqual((rc, out), (3, b"abc"))

    def test_hung_process_is_killed(self):
        code = "import time; time.sleep(30)"
        rc, _, _ = asyncio.run(run_process([sys.executable, "-c", code], b"", 0.3))
        self.assertEqual(rc, 124)


if __name__ == "__main__":
    unittest.main()
