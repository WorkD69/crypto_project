"""Opt-in real browser tests on local synthetic DOM, no Telegram login/network."""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from scripts.collector.telegram import EXTRACT_JS, matching_search_results, profile_links
from scripts.collector.media import download_media


@unittest.skipUnless(os.environ.get("COLLECTOR_BROWSER_TESTS") == "1", "Set COLLECTOR_BROWSER_TESTS=1 with optional Playwright installed")
class RealBrowserContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel="chromium", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page(accept_downloads=True)
        self.addCleanup(self.page.close)

    def test_extract_text_without_meta_and_preserve_observed_ids_and_album(self):
        self.page.set_content('''<div id="MiddleColumn"><div class="Message" data-message-id="42" data-grouped-id="visible-album">
          <div class="bottom-marker" data-album-main-id="42" data-last-message-id="43"></div>
          <div class="text-content">BTCUSDT<br>Caption<span class="MessageMeta">99 views<span class="message-time" title="Oct 12, 2026, 23:39:45">23:39</span></span></div>
          <div class="media-inner"><img class="full-media"></div>
          <div class="media-inner"><video></video></div></div></div>''')
        row = self.page.evaluate(EXTRACT_JS)[0]
        self.assertEqual(row["text"], "BTCUSDT\nCaption")
        self.assertEqual(row["message_id"], "42")
        self.assertEqual(row["album_id"], "visible-album")
        self.assertEqual([m["kind"] for m in row["media"]], ["photo", "video"])
        self.assertEqual(row["media"][0]["dom_album_main_message_id"], "42")
        self.assertEqual(row["media"][1]["dom_album_last_message_id"], "43")

    def test_standard_download_saved_by_hash(self):
        self.page.set_content('''<div id="MiddleColumn"><div class="Message" data-message-id="42"><div class="media-inner" onclick="document.querySelector('a').style.display='block'">Open own media</div></div></div>
          <a role="button" aria-label="Download" style="display:none" download="../../test.png" href="data:application/octet-stream;base64,YWJj">Download</a>''')
        with tempfile.TemporaryDirectory() as directory:
            result = download_media(self.page, "42", {"position": 0}, Path(directory), 1)
            self.assertEqual(result["state"], "DOWNLOADED")
            self.assertEqual(result["sha256"], hashlib.sha256(b"abc").hexdigest())
            self.assertEqual((Path(directory) / result["path"]).read_bytes(), b"abc")

    def test_missing_visible_message_defers_download(self):
        self.page.set_content('<div id="MiddleColumn"></div>')
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(download_media(self.page, "42", {"position": 0}, directory, 1)["state"], "DEFERRED")

    def test_download_closes_viewer_before_next_media(self):
        self.page.set_content('''<div id="MiddleColumn"><div class="Message" data-message-id="42">
          <div class="media-inner" onclick="document.querySelector('#MediaViewer').showModal()">Open media</div>
          </div></div>
          <dialog id="MediaViewer" oncancel="event.preventDefault()"><a aria-label="Download" download="own.jpg"
          href="data:application/octet-stream;base64,YWJj">Download</a>
          <button aria-label="Close" onclick="document.querySelector('#MediaViewer').close()">Close</button></dialog>''')
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(download_media(self.page, "42", {"position": 0}, directory, 1)["state"], "DOWNLOADED")
        self.assertEqual(self.page.locator('#MediaViewer[open]').count(), 0)

    def test_fresh_browser_profile_shows_login_state_from_dom(self):
        from scripts.collector.browser import read_session_state
        self.page.set_content('<div id="auth-qr-form"></div>')
        self.assertEqual(read_session_state(self.page), "AUTH_REQUIRED")

    def test_only_profile_link_row_can_verify_username(self):
        self.page.set_content('''<div id="RightColumn">
          <div class="ListItem"><span class="title">https://t.me/fake_from_description</span><span class="subtitle">Bio</span></div>
          <div class="ListItem"><span class="title">https://t.me/wyckoffcompany</span><span class="subtitle">Link</span></div></div>''')
        self.assertEqual(profile_links(self.page), ["https://t.me/wyckoffcompany"])

    def test_search_does_not_select_stale_result_from_previous_channel(self):
        self.page.set_content('''<div id="LeftColumn">
          <div class="search-result"><span class="title">КРИПТО ИЛЬЯ</span></div>
          <div class="search-result"><span class="title">Мысли Вайкоффа</span></div>
        </div>''')
        result = matching_search_results(self.page, "Мысли Вайкоффа")
        self.assertEqual(result.count(), 1)
        self.assertEqual(result.first.inner_text(), "Мысли Вайкоффа")

    def test_waits_for_updating_to_finish_and_new_tail_to_arrive(self):
        from scripts.collector.telegram import wait_for_messages_ready
        self.page.set_content('''<div id="MiddleColumn"><div class="ChatInfo"><span class="status">updating</span></div>
          <div class="MessageList"><div class="Message" data-message-id="42"><div class="text-content">old</div></div></div></div>
          <script>setTimeout(()=>{document.querySelector('.MessageList').insertAdjacentHTML('beforeend',
          '<div class="Message" data-message-id="43"><div class="text-content">new</div></div>');
          document.querySelector('.status').remove()},500)</script>''')
        evidence = wait_for_messages_ready(self.page, timeout_seconds=4, quiet_seconds=.4, minimum_seconds=.6)
        self.assertEqual(evidence['state'], 'UI_READY')
        self.assertEqual(evidence['latest_id'], '43')
        self.assertFalse(evidence['server_sync_confirmed'])

    def test_busy_ui_does_not_claim_success(self):
        from scripts.collector.telegram import wait_for_messages_ready
        self.page.set_content('<div id="MiddleColumn"><div class="ChatInfo"><span class="status">updating</span></div><div class="MessageList"><div class="Message" data-message-id="42"></div></div></div>')
        with self.assertRaisesRegex(RuntimeError, 'SYNC_UNCONFIRMED'):
            wait_for_messages_ready(self.page, timeout_seconds=.8, quiet_seconds=.2, minimum_seconds=.2)


if __name__ == "__main__":
    unittest.main()
