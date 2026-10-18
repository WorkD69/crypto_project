"""Explicit live fixtures in Saved Messages only. Never used by the scheduled reader."""
import argparse
import json
import re
import time
import uuid

from scripts.collector.browser import open_browser, wait_for_session
from scripts.collector.config import ROOT, load_config, runtime_root
from scripts.collector.telegram import open_source


def require_own_fixture(message_id, text):
    if not re.fullmatch(r"[1-9][0-9]*", str(message_id)) or not text.startswith("CRP-STAGE0"):
        raise ValueError("Refusing to edit anything except a marked own Stage 0 fixture")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seed", "new", "edit", "photo", "video", "album", "hermes-test"))
    parser.add_argument("--message-id")
    args = parser.parse_args()
    prefix = f"CRP-STAGE0 {'HERMES-FIRST' if args.action == 'hermes-test' else args.action} {uuid.uuid4().hex[:8]}: "
    text = prefix + "BTCUSDT 4H; liquidity, FVG. Собственный тест программы. Ценовых уровней и торгового сигнала нет."
    with open_browser(runtime_root()) as (context, page):
        if wait_for_session(page) != "AUTHENTICATED":
            raise RuntimeError("AUTH_REQUIRED")
        identity = open_source(page, load_config()["sources"][0])
        if identity["title"] != "Saved Messages":
            raise RuntimeError("IDENTITY_MISMATCH")
        page.locator('#editable-message-text').wait_for()
        page.wait_for_timeout(600)
        draft = page.locator('#editable-message-text').inner_text().strip()
        cancel = page.get_by_role("button", name="Cancel", exact=True)
        if cancel.count() and cancel.is_visible():
            if not draft.startswith("CRP-STAGE0"):
                raise RuntimeError("UNRELATED_DRAFT")
            cancel.click()
        elif draft and not draft.startswith("CRP-STAGE0"):
            raise RuntimeError("UNRELATED_DRAFT")
        if args.action == "edit":
            if not args.message_id or not re.fullmatch(r"[1-9][0-9]*", args.message_id):
                parser.error("edit requires a stable --message-id")
            target = page.locator(f'#MiddleColumn .Message[data-message-id="{args.message_id}"]')
            require_own_fixture(args.message_id, target.locator('.text-content').inner_text().strip())
            target.locator('.text-content').click(button="right", no_wait_after=True)
            page.locator('.MenuItem').get_by_text("Edit", exact=True).click()
            save = page.get_by_role("button", name="Save edited message", exact=True)
            save.wait_for()
            page.locator('#editable-message-text').fill(text)
            save.click(no_wait_after=True)
        elif args.action in ("seed", "new", "hermes-test"):
            page.locator('#editable-message-text').fill(text)
            page.locator('#editable-message-text').press("Enter")
        else:
            files = [ROOT / "artifacts" / name for name in (
                ["test_video.mp4"] if args.action == "video" else
                ["btc_test_chart.png", "unreadable_test.png"] if args.action == "album" else ["btc_test_chart.png"])]
            page.get_by_role("button", name="Add attachment", exact=True).click()
            with page.expect_file_chooser() as chooser:
                page.get_by_text("Photo or Video", exact=True).click()
            chooser.value.set_files(files)
            page.locator('#editable-message-text-modal').fill(text)
            page.locator('[role="dialog"] button.primary.inline').click(no_wait_after=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if page.evaluate("""prefix => [...document.querySelectorAll('#MiddleColumn .Message[data-message-id]')]
                .some(e => e.innerText.includes(prefix) && /^[1-9][0-9]*$/.test(e.getAttribute('data-message-id')))""", prefix):
                break
            page.locator('#MiddleColumn .MessageList').last.evaluate('e => e.scrollTop=e.scrollHeight')
            page.wait_for_timeout(300)
        else:
            raise RuntimeError("SEND_UNCONFIRMED_DO_NOT_AUTOMATICALLY_RESEND")
        message = page.locator('#MiddleColumn .Message').filter(has_text=prefix).last
        message_id = message.get_attribute('data-message-id')
        if args.action == "edit" and message_id != args.message_id:
            raise RuntimeError("EDIT_DID_NOT_PRESERVE_MESSAGE_ID")
        page.wait_for_timeout(600)
        result = {"action": args.action, "message_id": message_id,
                  "media_containers": message.locator('.media-inner').count(),
                  "own_saved_messages_only": True}
        print(json.dumps(result), flush=True)
        target = runtime_root() / "fixtures.jsonl"
        with target.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
