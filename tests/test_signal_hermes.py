import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from signal_bot.hermes import BadAnswer, BrokerClient, Gate, HermesError, parse_stream, write_spool


def stream(text, exit_code=0, extra=()):
    records = [{"type": "system", "subtype": "init"}, *extra,
               {"type": "result", "exit_code": exit_code, "text": text, "duration_ms": 900,
                "tokens": {"input": 100, "output": 20, "total": 120}}]
    return "\n".join(json.dumps(record, ensure_ascii=False) for record in records)


class ParseStreamTest(unittest.TestCase):
    def test_final_answer_and_usage(self):
        reply = parse_stream(stream('{"relevant": false}'))
        self.assertEqual(reply.answer, {"relevant": False})
        self.assertEqual(reply.tokens["total"], 120)
        self.assertEqual(reply.duration_ms, 900)

    def test_markdown_fence_is_removed(self):
        self.assertEqual(parse_stream(stream('```json\n{"a": 1}\n```')).answer, {"a": 1})

    def test_non_breaking_spaces_are_normalized(self):
        self.assertEqual(parse_stream(stream('{ "a": "84 000"}')).answer, {"a": "84 000"})

    def test_one_surplus_closing_brace_is_tolerated(self):
        self.assertEqual(parse_stream(stream('{"a": 1}}')).answer, {"a": 1})

    def test_other_trailing_text_is_rejected(self):
        with self.assertRaises(BadAnswer):
            parse_stream(stream('{"a": 1}, {"b": 2}'))

    def test_plain_text_answer_is_rejected(self):
        with self.assertRaises(BadAnswer):
            parse_stream(stream("Не могу прочитать график"))

    def test_json_that_is_not_an_object_is_rejected(self):
        with self.assertRaises(BadAnswer):
            parse_stream(stream("[1, 2]"))

    def test_native_tool_use_fails_closed(self):
        with self.assertRaises(HermesError) as caught:
            parse_stream(stream('{"a": 1}', extra=[{"type": "tool_use", "name": "terminal"}]))
        self.assertEqual(caught.exception.code, "UNEXPECTED_TOOL_USE")

    def test_failed_result_is_not_trusted(self):
        with self.assertRaises(HermesError) as caught:
            parse_stream(stream('{"a": 1}', exit_code=1))
        self.assertEqual(caught.exception.code, "RESULT_UNCONFIRMED")

    def test_missing_result_is_not_trusted(self):
        with self.assertRaises(HermesError):
            parse_stream('{"type": "text", "text": "{}"}\nnot json at all')


class FakeBroker:
    """Answers queued replies through the real newline-delimited protocol."""

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    async def connect(self):
        reader = asyncio.StreamReader()
        return reader, self.Writer(self, reader)

    class Writer:
        def __init__(self, broker, reader):
            self.broker, self.reader = broker, reader

        def write(self, data):
            self.broker.requests.append(json.loads(data))
            self.reader.feed_data(json.dumps(self.broker.replies.pop(0)).encode() + b"\n")
            self.reader.feed_eof()

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass


class ClientTest(unittest.TestCase):
    def ask(self, replies, **options):
        broker = FakeBroker(replies)
        client = BrokerClient(connect=broker.connect)
        return asyncio.run(client.ask("разбери", **options)), broker

    def test_request_holds_only_prompt_and_image(self):
        _, broker = self.ask([{"ok": True, "stream": stream('{"a": 1}')}], image_sha256="ab")
        self.assertEqual(broker.requests, [{"prompt": "разбери", "image_sha256": "ab"}])

    def test_broker_refusal_becomes_error_with_its_code(self):
        with self.assertRaises(HermesError) as caught:
            self.ask([{"ok": False, "error": "DAILY_LIMIT"}])
        self.assertEqual(caught.exception.code, "DAILY_LIMIT")

    def test_invalid_json_is_retried_once(self):
        reply, broker = self.ask([{"ok": True, "stream": stream("мусор")}, {"ok": True, "stream": stream('{"a": 1}')}])
        self.assertEqual(reply.answer, {"a": 1})
        self.assertEqual(len(broker.requests), 2)

    def test_second_invalid_json_is_final(self):
        broker = FakeBroker([{"ok": True, "stream": stream("мусор")}] * 3)
        with self.assertRaises(BadAnswer):
            asyncio.run(BrokerClient(connect=broker.connect).ask("разбери"))
        self.assertEqual(len(broker.requests), 2)

    def test_answer_failing_validation_is_retried_once(self):
        def validate(answer):
            if "zones" not in answer:
                raise BadAnswer("SCHEMA")
            return answer

        reply, broker = self.ask([{"ok": True, "stream": stream('{"a": 1}')},
                                  {"ok": True, "stream": stream('{"zones": []}')}], validate=validate)
        self.assertEqual(reply.parsed, {"zones": []})
        self.assertEqual(len(broker.requests), 2)

    def test_unreachable_broker_is_reported(self):
        async def refused():
            raise FileNotFoundError

        with self.assertRaises(HermesError) as caught:
            asyncio.run(BrokerClient(connect=refused).ask("разбери"))
        self.assertEqual(caught.exception.code, "BROKER_DOWN")

    def test_timeout_is_not_retried(self):
        broker = FakeBroker([{"ok": False, "error": "TIMEOUT"}, {"ok": True, "stream": stream("{}")}])
        with self.assertRaises(HermesError):
            asyncio.run(BrokerClient(connect=broker.connect).ask("разбери"))
        self.assertEqual(len(broker.requests), 1)


class GateTest(unittest.TestCase):
    def test_waiting_live_post_overtakes_waiting_history(self):
        async def scenario():
            gate, order = Gate(), []
            release = asyncio.Event()

            async def first():
                async with gate.slot(live=True):
                    await release.wait()

            async def job(name, live):
                async with gate.slot(live=live):
                    order.append(name)

            tasks = [asyncio.create_task(first())]
            await asyncio.sleep(0)
            tasks.append(asyncio.create_task(job("history", False)))
            await asyncio.sleep(0)
            tasks.append(asyncio.create_task(job("live", True)))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(*tasks)
            return order

        self.assertEqual(asyncio.run(scenario()), ["live", "history"])

    def test_same_priority_keeps_arrival_order(self):
        async def scenario():
            gate, order = Gate(), []

            async def job(name):
                async with gate.slot(live=True):
                    await asyncio.sleep(0)
                    order.append(name)

            await asyncio.gather(*(job(name) for name in "abc"))
            return order

        self.assertEqual(asyncio.run(scenario()), ["a", "b", "c"])

    def test_cancelled_waiter_does_not_block_the_queue(self):
        async def scenario():
            gate, order = Gate(), []
            release = asyncio.Event()

            async def first():
                async with gate.slot(live=True):
                    await release.wait()

            async def job(name):
                async with gate.slot(live=True):
                    order.append(name)

            holder = asyncio.create_task(first())
            await asyncio.sleep(0)
            lost = asyncio.create_task(job("lost"))
            await asyncio.sleep(0)
            kept = asyncio.create_task(job("kept"))
            await asyncio.sleep(0)
            lost.cancel()
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(holder, kept)
            return order

        self.assertEqual(asyncio.run(asyncio.wait_for(scenario(), 2)), ["kept"])


class SpoolTest(unittest.TestCase):
    def test_file_is_named_by_content_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            sha = write_spool(Path(folder), b"\xff\xd8\xffdata")
            self.assertEqual(len(sha), 64)
            self.assertEqual((Path(folder) / f"{sha}.jpg").read_bytes(), b"\xff\xd8\xffdata")


if __name__ == "__main__":
    unittest.main()
