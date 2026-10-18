"""Only Telegram's visible media viewer and standard Download action."""
import hashlib
from pathlib import Path


def reusable_media(media, root):
    if not media or media.get('state') != 'DOWNLOADED' or not media.get('sha256') or not media.get('path'):
        return False
    try:
        root = Path(root).resolve()
        path = (root / media['path']).resolve()
        if not path.is_relative_to(root / 'media') or not path.is_file():
            return False
        with path.open('rb') as stream:
            return hashlib.file_digest(stream,'sha256').hexdigest() == media['sha256']
    except (OSError,ValueError):
        return False


def same_media_structure(a, b):
    def structure(item):
        return (item.get('album_id'), [tuple(m.get(k) for k in
                ('position','kind','label','observed_id')) for m in item.get('media',[])])
    return structure(a) == structure(b)


def close_media_viewer(page):
    viewer = page.locator('#MediaViewer[open]')
    if not viewer.count():
        return True
    try:
        close = viewer.locator('[aria-label="Close"]').first
        if close.count() and close.is_visible():
            close.click(no_wait_after=True, timeout=3000)
        else:
            page.keyboard.press("Escape")
        viewer.wait_for(state="hidden", timeout=5000)
        return True
    except Exception:
        return False


def download_media(page, message_id, media, root, max_mb):
    # Never fetch service-worker/progressive URLs, auth keys or application internals.
    if not close_media_viewer(page):
        raise RuntimeError("VIEWER_CLOSE_FAILED")
    item = page.locator(f'#MiddleColumn .Message[data-message-id="{message_id}"] .media-inner').nth(media["position"])
    if not item.count():
        return {"state": "DEFERRED", "error": "NOT_IN_VISIBLE_DOM"}
    opened = False
    step = "OPEN_VIEWER"
    try:
        item.click(timeout=5000, no_wait_after=True)
        opened = True
        step = "WAIT_DOWNLOAD_ACTION"
        # The viewer mounts after the click. Do not choose a fallback before it exists.
        button = page.locator('a[download][aria-label="Download"], button[aria-label="Download"]').last
        button.wait_for(state="visible", timeout=12000)
        step = "DOWNLOAD_EVENT"
        with page.expect_download(timeout=30000) as event:
            button.click(no_wait_after=True)
        download = event.value
        step = "TRANSFER"
        target_dir = Path(root) / "media"
        target_dir.mkdir(parents=True, exist_ok=True)
        # No untrusted remote filename can determine the local destination.
        temp = target_dir / "download.pending"
        download.save_as(temp)
        if temp.stat().st_size > max_mb * 1024 * 1024:
            temp.unlink()
            return {"state": "TOO_LARGE", "error": "MEDIA_SIZE_LIMIT"}
        with temp.open("rb") as stream:
            sha = hashlib.file_digest(stream, "sha256").hexdigest()
        suffix = Path(download.suggested_filename).suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".webm", ".gif"):
            suffix = ".bin"
        destination = target_dir / (sha + suffix)
        temp.replace(destination)
        return {"state": "DOWNLOADED", "path": destination.relative_to(root).as_posix(),
                "sha256": sha, "bytes": destination.stat().st_size,
                "acquisition": "telegram_visible_download_button"}
    except Exception as exc:
        return {"state": "UNAVAILABLE", "error": type(exc).__name__, "step": step}
    finally:
        if opened and not close_media_viewer(page):
            # Do not process another item while this viewer could still point
            # at the previous message's Download action.
            raise RuntimeError("VIEWER_CLOSE_FAILED")
