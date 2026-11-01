"""Narrow gateway to Hermes: one fixed command, one call at a time, a daily budget."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("signal_bot.broker")

SOCKET = Path("/run/crypto-hermes/broker.sock")
SPOOL = Path("/var/lib/crypto-signals/spool")
STATE = Path("/var/lib/crypto-hermes-broker/state.json")
CONTAINER_SPOOL = "/opt/data/crypto-spool"

CALL_TIMEOUT = 120
PROCESS_TIMEOUT = CALL_TIMEOUT + 10
COPY_TIMEOUT = 30
DAILY_LIMIT = 300
LIMIT_PAUSE = timedelta(hours=1)
MAX_PROMPT = 32_000
MAX_REQUEST = 128 * 1024
MAX_IMAGE = 10 * 1024 * 1024
TIMEOUT_CODES = {124, 137}

DOCKER = ["docker", "exec", "-i", "-u", "hermes", "hermes-agent"]
# docker exec does not stop the process in the container when the client dies, so the limit runs inside.
HERMES = [*DOCKER, "timeout", "-k", "5", str(CALL_TIMEOUT), "/opt/hermes/.venv/bin/hermes", "chat",
          "--query-file", "-", "--oneshot", "-Q", "--ignore-rules", "--source", "tool", "--toolsets", "no_mcp",
          "--max-turns", "1", "--reasoning", "low", "-m", "gpt-6-luna", "--format", "stream-json"]
COPY_SCRIPT = 'umask 077; mkdir -p "${0%/*}"; cat > "$0"'

SHA_RE = re.compile(r"[0-9a-f]{64}")
REFERENCE_RE = re.compile(r"(?<![\w/])@(?=[a-zA-Z])")
LIMIT_RE = re.compile(r"rate.?limit|usage.?limit|too many requests|\b429\b|quota", re.I)


def image_path(sha: str) -> str:
    return f"{CONTAINER_SPOOL}/{sha}.jpg"


def command(image_sha256: str | None) -> list[str]:
    return [*HERMES, "--image", image_path(image_sha256)] if image_sha256 else list(HERMES)


def neutralize(prompt: str) -> str:
    """Author text must never become a Hermes @file or @url reference."""
    return REFERENCE_RE.sub("@​", prompt)


def parse_request(raw: bytes) -> tuple[str, str | None] | None:
    try:
        request = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(request, dict) or not set(request) <= {"prompt", "image_sha256"}:
        return None
    prompt, sha = request.get("prompt"), request.get("image_sha256")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
        return None
    if sha is not None and not (isinstance(sha, str) and SHA_RE.fullmatch(sha)):
        return None
    return prompt, sha


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Budget:
    """Daily call counter and limit pause. Stored on disk to survive restarts."""

    def __init__(self, path: Path, limit: int = DAILY_LIMIT, clock=utc_now):
        self.path, self.limit, self.clock = path, limit, clock

    def _load(self) -> dict:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        today = self.clock().date().isoformat()
        calls = state.get("calls", 0) if state.get("day") == today else 0
        return {"day": today, "calls": calls, "paused_until": state.get("paused_until")}

    def _save(self, state: dict) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        os.replace(temporary, self.path)

    def refusal(self) -> str | None:
        state = self._load()
        paused = state["paused_until"]
        if paused and datetime.fromisoformat(paused) > self.clock():
            return "PAUSED"
        return "DAILY_LIMIT" if state["calls"] >= self.limit else None

    def spend(self) -> int:
        state = self._load()
        state["calls"] += 1
        self._save(state)
        return state["calls"]

    def pause(self, length: timedelta) -> None:
        state = self._load()
        state["paused_until"] = (self.clock() + length).isoformat()
        self._save(state)


async def run_process(argv: list[str], stdin: bytes, timeout: float) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return 124, b"", b""
    return process.returncode, stdout, stderr


def failure_text(stdout: bytes, stderr: bytes) -> str:
    """Only the error field and stderr count: answers quote author text with any words in it."""
    error = ""
    for line in stdout.decode("utf-8", "replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("type") == "result":
            error = str(record.get("error", ""))
    return error + "\n" + stderr.decode("utf-8", "replace")


class Broker:
    def __init__(self, spool: Path, budget: Budget, run=run_process):
        self.spool, self.budget, self.run = spool, budget, run
        self.lock = asyncio.Lock()

    def _image(self, sha: str) -> bytes | None:
        path = self.spool / f"{sha}.jpg"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_IMAGE:
            return None
        data = path.read_bytes()
        return data if data[:3] == b"\xff\xd8\xff" and hashlib.sha256(data).hexdigest() == sha else None

    async def _ask_model(self, prompt: str, sha: str | None) -> dict:
        calls = self.budget.spend()
        started = asyncio.get_running_loop().time()
        rc, stdout, stderr = await self.run(command(sha), neutralize(prompt).encode("utf-8"), PROCESS_TIMEOUT)
        log.info("call %d rc=%d seconds=%.1f image=%s", calls, rc,
                 asyncio.get_running_loop().time() - started, bool(sha))
        if rc == 0:
            return {"ok": True, "stream": stdout.decode("utf-8", "replace")}
        if rc in TIMEOUT_CODES:
            return {"ok": False, "error": "TIMEOUT"}
        if LIMIT_RE.search(failure_text(stdout, stderr)):
            self.budget.pause(LIMIT_PAUSE)
            log.warning("limit signs found, pausing for %s", LIMIT_PAUSE)
            return {"ok": False, "error": "RATE_LIMITED"}
        return {"ok": False, "error": "HERMES_FAILED"}

    async def _with_image(self, prompt: str, sha: str, image: bytes) -> dict:
        target = image_path(sha)
        try:
            rc, _, _ = await self.run([*DOCKER, "sh", "-c", COPY_SCRIPT, target], image, COPY_TIMEOUT)
            if rc != 0:
                log.warning("image copy failed rc=%d", rc)
                return {"ok": False, "error": "HERMES_FAILED"}
            return await self._ask_model(prompt, sha)
        finally:
            await self.run([*DOCKER, "rm", "-f", target], b"", COPY_TIMEOUT)

    async def handle(self, raw: bytes) -> dict:
        request = parse_request(raw)
        if request is None:
            return {"ok": False, "error": "BAD_REQUEST"}
        prompt, sha = request
        async with self.lock:
            refusal = self.budget.refusal()
            if refusal:
                return {"ok": False, "error": refusal}
            if sha is None:
                return await self._ask_model(prompt, None)
            image = self._image(sha)
            if image is None:
                return {"ok": False, "error": "IMAGE_REJECTED"}
            return await self._with_image(prompt, sha, image)


async def serve() -> None:
    broker = Broker(SPOOL, Budget(STATE, int(os.environ.get("BROKER_DAILY_LIMIT", DAILY_LIMIT))))

    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            reply = await broker.handle(await reader.readline())
        except ValueError:  # a line longer than the stream limit
            reply = {"ok": False, "error": "BAD_REQUEST"}
        writer.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(on_client, str(SOCKET), limit=MAX_REQUEST)
    os.chmod(SOCKET, 0o660)
    log.info("broker is listening")
    async with server:
        await server.serve_forever()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(serve())


if __name__ == "__main__":
    main()
