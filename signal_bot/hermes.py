"""Client of the Hermes broker and parsing of the model answer."""
from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from functools import partial
from itertools import count
from pathlib import Path
from typing import Any, Callable

SOCKET = "/run/crypto-hermes/broker.sock"
CLIENT_TIMEOUT = 180
MAX_REPLY = 16 * 1024 * 1024
SPACES = str.maketrans({" ": " ", " ": " ", " ": " "})


class HermesError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class BadAnswer(HermesError):
    """The model answered, but not with what was asked. Worth one more try."""


@dataclass(frozen=True)
class Reply:
    answer: dict
    tokens: dict
    duration_ms: int
    parsed: Any = None


def _decode(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        value, end = json.JSONDecoder().raw_decode(text)
    except ValueError:
        raise BadAnswer("INVALID_JSON") from None
    # A single surplus closing brace cannot hide another value or instruction.
    if text[end:] != "}":
        raise BadAnswer("INVALID_JSON")
    return value


def parse_stream(stream: str) -> Reply:
    records = []
    for line in stream.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    if any(record.get("type") == "tool_use" for record in records):
        raise HermesError("UNEXPECTED_TOOL_USE")
    terminal = next((record for record in reversed(records) if record.get("type") == "result"), None)
    if terminal is None or terminal.get("exit_code"):
        raise HermesError("RESULT_UNCONFIRMED")
    text = terminal.get("text", "").translate(SPACES).strip()
    if text.startswith("```") and text.endswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    answer = _decode(text)
    if not isinstance(answer, dict):
        raise BadAnswer("NOT_AN_OBJECT")
    return Reply(answer, terminal.get("tokens", {}), terminal.get("duration_ms", 0))


class Gate:
    """One call at a time; a waiting live post overtakes waiting history."""

    def __init__(self):
        self.busy = False
        self.waiting: list = []
        self.order = count()

    def _release(self) -> None:
        while self.waiting:
            _, _, turn = heapq.heappop(self.waiting)
            if not turn.done():
                turn.set_result(None)
                return
        self.busy = False

    @asynccontextmanager
    async def slot(self, live: bool = True):
        if self.busy:
            turn = asyncio.get_running_loop().create_future()
            heapq.heappush(self.waiting, (0 if live else 1, next(self.order), turn))
            try:
                await turn
            except asyncio.CancelledError:
                if turn.done() and not turn.cancelled():
                    self._release()
                raise
        self.busy = True
        try:
            yield
        finally:
            self._release()


class BrokerClient:
    def __init__(self, path: str = SOCKET, connect: Callable | None = None):
        self.connect = connect or partial(asyncio.open_unix_connection, path, limit=MAX_REPLY)
        self.gate = Gate()

    async def _call(self, prompt: str, image_sha256: str | None) -> str:
        request = json.dumps({"prompt": prompt, "image_sha256": image_sha256}, ensure_ascii=False)
        try:
            reader, writer = await self.connect()
            try:
                writer.write(request.encode("utf-8") + b"\n")
                await writer.drain()
                line = await asyncio.wait_for(reader.readline(), CLIENT_TIMEOUT)
            finally:
                writer.close()
                await writer.wait_closed()
        except TimeoutError:
            raise HermesError("BROKER_TIMEOUT") from None
        except (OSError, ValueError):
            raise HermesError("BROKER_DOWN") from None
        if not line:
            raise HermesError("BROKER_DOWN")
        reply = json.loads(line)
        if not reply.get("ok"):
            raise HermesError(reply.get("error", "BROKER_FAILED"))
        return reply["stream"]

    async def _attempt(self, prompt: str, image_sha256: str | None, validate: Callable | None) -> Reply:
        reply = parse_stream(await self._call(prompt, image_sha256))
        return replace(reply, parsed=validate(reply.answer)) if validate else reply

    async def ask(self, prompt: str, image_sha256: str | None = None, validate: Callable | None = None,
                  live: bool = True) -> Reply:
        async with self.gate.slot(live):
            try:
                return await self._attempt(prompt, image_sha256, validate)
            except BadAnswer:
                return await self._attempt(prompt, image_sha256, validate)


def write_spool(folder: Path, data: bytes) -> str:
    """Store an image by its hash so the broker, in the same group, can read it."""
    sha = hashlib.sha256(data).hexdigest()
    target = folder / f"{sha}.jpg"
    temporary = folder / f"{sha}.tmp"
    temporary.write_bytes(data)
    os.chmod(temporary, 0o640)
    os.replace(temporary, target)
    return sha
