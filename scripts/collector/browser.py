"""Dedicated persistent profile; visible UI only, no exported auth or Telegram RPC."""
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

TELEGRAM_WEB = "https://web.telegram.org/a/"


def protect_runtime(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    # Also protects this directory if an ancestor was accidentally made a Git root.
    (root / ".gitignore").write_text("*\n", encoding="utf-8")
    if os.name == "nt":
        identity = subprocess.check_output(["whoami"], text=True).strip()
        subprocess.run(["icacls", str(root), "/inheritance:r", "/grant:r",
                        identity + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
                       check=True, capture_output=True)
    else:
        root.chmod(0o700)


class ProfileLock:
    def __init__(self, root):
        self.path = Path(root) / "collector.lock"
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("PROFILE_BUSY") from exc
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close()  # OS releases the lock even after a crashed process.


def session_state(flags):
    if flags.get("online") is False:
        return "NETWORK_UNAVAILABLE"
    if flags.get("login"):
        return "AUTH_REQUIRED"
    return "AUTHENTICATED" if flags.get("chat_list") else "UI_UNRECOGNIZED"


def read_session_state(page):
    return session_state(page.evaluate("""() => ({
        online: navigator.onLine,
        login: !!document.querySelector('#auth-qr-form, #auth-phone-number-form, .auth-form, .Auth'),
        chat_list: !!document.querySelector('#LeftColumn .chat-list, #LeftColumn .ChatList, #LeftColumn .chat-list-wrapper, #LeftColumn .ListItem.Chat')
    })"""))


def wait_for_session(page, seconds=30, allow_login=False):
    deadline = time.monotonic() + seconds
    state = "UI_UNRECOGNIZED"
    while time.monotonic() < deadline:
        state = read_session_state(page)
        if state in ("AUTHENTICATED", "NETWORK_UNAVAILABLE") or (state == "AUTH_REQUIRED" and not allow_login):
            return state
        page.wait_for_timeout(500)
    return state


@contextmanager
def open_browser(root, headless=False):
    from playwright.sync_api import sync_playwright
    protect_runtime(root)
    with ProfileLock(root), sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path(root) / "profile"), headless=headless,
            channel="chromium", accept_downloads=True, viewport={"width": 1280, "height": 900},
            locale="en-US", timezone_id="Europe/Moscow", chromium_sandbox=True)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(8000)
            page.goto(TELEGRAM_WEB, wait_until="domcontentloaded", timeout=45000)
            yield context, page
        finally:
            context.close()
