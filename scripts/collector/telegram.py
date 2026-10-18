"""Bounded Telegram Web A UI adapter; no Telegram client keys/internal stores."""
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.collector.store import normalize_message

EXTRACT_JS = Path(__file__).with_name("extract_dom.js").read_text(encoding="utf-8")
MESSAGES = '#MiddleColumn .Message[data-message-id]'


def parse_ui_time(value):
    # Browser locale and timezone are explicitly pinned in browser.py.
    if not value:
        return None
    match = re.fullmatch(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4}), (\d{2}):(\d{2}):(\d{2})", value)
    if not match:
        return None
    month, day, year, hour, minute, second = match.groups()
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    try:
        dt = datetime(int(year), months.index(month) + 1, int(day), int(hour), int(minute), int(second),
                      tzinfo=timezone(timedelta(hours=3)))
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def coverage_state(ids, anchor, reached_start):
    if not anchor:
        return "BOUNDED_INITIAL"
    return "COMPLETE" if anchor in ids or reached_start else "PARTIAL"


def profile_links(page):
    return page.locator('#RightColumn .ListItem:visible').evaluate_all("""es => es
        .filter(e => e.querySelector('.subtitle')?.textContent.trim() === 'Link')
        .map(e => e.querySelector('.title')?.textContent.trim()).filter(Boolean)""")


def matching_search_results(page, expected_title):
    # Telegram retains the previous query's visible results while the next
    # search is pending. A generic first-result click can reopen that peer.
    return page.locator('#LeftColumn .search-result:visible').filter(
        has=page.get_by_text(expected_title, exact=True))


def validate_source_route(kind, route):
    if not re.fullmatch(r"-?[0-9]+", route) or (kind == "channel" and not route.startswith("-")):
        raise RuntimeError("SOURCE_KIND_MISMATCH")


def open_source(page, source):
    page.collector_step = "OPEN_SOURCE"
    if source["kind"] == "saved":
        page.get_by_role("button", name="Open menu", exact=True).click(no_wait_after=True)
        page.locator('.Menu.main-menu').get_by_text("Saved Messages", exact=True).click(no_wait_after=True)
    else:
        page.collector_step = "SEARCH_FOCUS"
        search = page.locator('#LeftColumn input[placeholder="Search"]')
        search.click()
        page.wait_for_timeout(400)
        search.fill("@" + source["username"] if source.get("username") else source.get("search_query", source["expected_title"]))
        page.collector_step = "WAIT_SEARCH_RESULT"
        # Wait for this query's visible title before clicking. Telegram Web can
        # otherwise leave a stale result from the previous query in the DOM.
        result = matching_search_results(page, source["expected_title"])
        result.first.wait_for(timeout=15000)
        old_url = page.url
        page.collector_step = "SELECT_SEARCH_RESULT"
        result.first.click(no_wait_after=True)
        try:
            page.wait_for_function("old => location.href !== old", arg=old_url, timeout=5000)
        except Exception:
            pass  # Same already-open channel is allowed, but identity still needs verification.
        page.wait_for_timeout(800)
    header = page.locator('#MiddleColumn .ChatInfo:visible .title').first
    page.collector_step = "WAIT_CHAT_HEADER"
    header.wait_for(timeout=15000)
    expected = "Saved Messages" if source["kind"] == "saved" else source["expected_title"]
    actual_title = header.inner_text().strip()
    if " ".join(actual_title.split()).casefold() != " ".join(expected.split()).casefold():
        raise RuntimeError("IDENTITY_MISMATCH")
    # Numeric peer identifier is observed in the browser location, never fabricated.
    route = page.url.split("#", 1)[-1]
    validate_source_route(source["kind"], route)
    identity = {"title": actual_title, "configured_title": expected, "web_peer_route": route, "username": None,
                "basis": "visible_chat_header_and_location", "verified": source["kind"] == "saved"}
    if source["kind"] != "saved":
        page.collector_step = "OPEN_CHANNEL_INFO"
        # The header opens Telegram's visible channel info, including username.
        info_heading = page.locator('#RightColumn').get_by_text("Channel Info", exact=True).first
        if not info_heading.count() or not info_heading.is_visible():
            header.click(no_wait_after=True)
        page.wait_for_timeout(1200)
        right = page.locator('#RightColumn')
        if right.count():
            page.collector_step = "VERIFY_PROFILE_LINK"
            links = profile_links(page)
            if source.get("username"):
                # The info panel may still be rendering the previous transition.
                for _ in range(10):
                    if any((link or "").lower().rstrip("/") == "https://t.me/" + source["username"].lower() for link in links):
                        break
                    page.wait_for_timeout(300)
                    links = profile_links(page)
            usernames = [m.group(1) for href in links
                         if (m := re.fullmatch(r"https?://t\.me/([A-Za-z0-9_]+)/?", href or ""))]
            if source.get("username"):
                if source["username"].lower() not in [u.lower() for u in usernames]:
                    raise RuntimeError("IDENTITY_MISMATCH")
                identity.update(username=source["username"], verified=True,
                                basis="visible_channel_info_username_and_location")
            else:
                identity["visible_usernames"] = usernames
                # A private invite alone cannot prove the peer matches the configured source.
                bound = source.get("expected_web_peer_route")
                identity["verified"] = bool(bound and bound == route)
                identity["basis"] = "title_and_owner_bound_peer" if bound else "title_only_needs_binding"
                if source.get("source_url") in links:
                    identity.update(verified=True, basis="visible_invite_link_and_location")
    return identity


def capture_visible(page, prefix=None, message_id=None):
    # Telegram lazily fills the exact timestamp tooltip on pointer entry.
    rows = page.evaluate(EXTRACT_JS)
    if message_id is not None:
        rows = [row for row in rows if row['message_id'] == message_id]
    for row in rows:
        if message_id is None and (prefix is None or (prefix and not row["text"].startswith(prefix))):
            continue
        mid = row["message_id"]
        if not mid or not re.fullmatch(r"[1-9][0-9]*", mid):
            continue
        time_label = page.locator(f'{MESSAGES}[data-message-id="{mid}"] .message-time').first
        try:
            if time_label.count():
                time_label.hover(force=True, timeout=1500)
                if not time_label.get_attribute("title"):
                    page.wait_for_timeout(100)
                row["original_time_raw"] = time_label.get_attribute("title") or row["original_time_raw"]
        except Exception:
            # Hover may unmount other rows in Telegram's virtualized list.
            row["warnings"].append("EXACT_TIMESTAMP_NOT_VISIBLE")
    return rows


def wait_for_messages_ready(page, timeout_seconds=60, quiet_seconds=2, minimum_seconds=4):
    """Wait for visible Telegram synchronization and a quiet latest tail.

    This is UI evidence, not a Telegram server sequence/pts acknowledgement.
    Never treat a cached static list during 'updating' as ready.
    """
    started=time.monotonic(); unchanged_since=started; last=None; busy_seen=False
    while time.monotonic()-started < timeout_seconds:
        flags=page.evaluate('''() => {
          const visible=e=>!!e.getClientRects().length;
          const statuses=[...document.querySelectorAll('#MiddleColumn .ChatInfo .status')].filter(visible).map(e=>e.textContent.trim().toLowerCase());
          const busy=statuses.some(s=>/updating|connecting|waiting for network|reconnecting|обновлен|соединен|подключен|ожидание сети/.test(s)) ||
            [...document.querySelectorAll('#MiddleColumn .MessageList .Spinner, #MiddleColumn .MessageList [role="progressbar"], #MiddleColumn .MessageList [aria-busy="true"]')].some(visible);
          const list=[...document.querySelectorAll('#MiddleColumn .MessageList')].at(-1);
          if(list)list.scrollTop=list.scrollHeight;
          return {online:navigator.onLine,busy,list:!!list,
            rows:[...document.querySelectorAll('#MiddleColumn .Message[data-message-id]')].map(e=>({id:e.getAttribute('data-message-id'),text:e.querySelector('.text-content')?.textContent,media:e.querySelectorAll('.media-inner').length}))};
        }''')
        if flags['online'] is False:raise RuntimeError('NETWORK_UNAVAILABLE')
        busy_seen=busy_seen or flags['busy']
        signature=json.dumps(flags['rows'],sort_keys=True)
        if flags['busy'] or not flags['list'] or not flags['rows'] or signature!=last:
            unchanged_since=time.monotonic()
        last=signature
        if not flags['busy'] and flags['list'] and flags['rows'] and \
                time.monotonic()-unchanged_since>=quiet_seconds and time.monotonic()-started>=minimum_seconds:
            ids=[r['id'] for r in flags['rows'] if re.fullmatch(r'[1-9][0-9]*',r['id'] or '')]
            return {'state':'UI_READY','server_sync_confirmed':False,'busy_observed':busy_seen,
                    'wait_seconds':round(time.monotonic()-started,2),'latest_id':max(ids,key=int,default=None)}
        page.wait_for_timeout(200)
    raise RuntimeError('SYNC_UNCONFIRMED')


def scan_source(page, store, root, config, source, identity, recheck_media=False, target_message_id=None, metadata_only=False):
    previous = store.cursor(source["key"])
    anchor = previous and previous["high_water_id"]
    if previous and previous["coverage"] == "PARTIAL":
        anchor = json.loads(previous["detail"]).get("catchup_anchor", anchor)
    # A source/account switch must never share an old cursor.
    old_identity = previous and json.loads(previous["detail"]).get("identity")
    if old_identity and old_identity["web_peer_route"] != identity["web_peer_route"]:
        raise RuntimeError("IDENTITY_MISMATCH")
    page.locator(MESSAGES).first.wait_for(timeout=20000)
    freshness=wait_for_messages_ready(page)
    scroll = page.locator('#MiddleColumn .MessageList').last
    scroll.evaluate("e => e.scrollTop = e.scrollHeight")
    page.wait_for_timeout(800)
    seen = {}
    reached_start = False
    unchanged = 0
    for _ in range(config["max_scrolls"]):
        rows = capture_visible(page, message_id=target_message_id)
        before = len(seen)
        for row in rows:
            mid = row["message_id"]
            if mid and re.fullmatch(r"[1-9][0-9]*", mid):
                seen[mid] = row
        if len(seen) >= config["max_messages"]:
            break
        if target_message_id and target_message_id in seen:
            break
        if anchor in seen and sum(int(mid) <= int(anchor) for mid in seen) >= config["overlap_messages"]:
            break
        unchanged = unchanged + 1 if len(seen) == before else 0
        if unchanged >= 3 and not target_message_id:
            # Stable DOM is not proof of the start: network/history may be unavailable.
            break
        scroll = page.locator('#MiddleColumn .MessageList').last
        if not scroll.count():
            break
        scroll.hover()
        page.mouse.wheel(0, -1100)
        page.wait_for_timeout(600)
    # Reconcile the latest tail after history scanning. New rows must join the
    # scan even if they arrived after the initial history snapshot.
    scroll=page.locator('#MiddleColumn .MessageList').last
    scroll.evaluate("e => e.scrollTop = e.scrollHeight")
    final_freshness=wait_for_messages_ready(page,minimum_seconds=2)
    for row in capture_visible(page, source.get("test_text_prefix", ""), message_id=target_message_id):
        if re.fullmatch(r'[1-9][0-9]*',row['message_id'] or ''):
            seen[row["message_id"]] = row
    ids = sorted(seen, key=int)[-config["max_messages"]:]
    coverage = 'TARGETED' if target_message_id else coverage_state(ids, anchor, reached_start)
    if target_message_id and target_message_id not in ids:
        raise RuntimeError('TARGET_MESSAGE_NOT_VISIBLE')
    counts = {"created": 0, "edited": 0, "duplicate": 0, "quarantined": 0}
    media_stats = {"downloaded": 0, "reused":0, "unavailable_or_deferred": 0}
    review_items = 0
    downloads = 0
    from scripts.collector.media import download_media, reusable_media, same_media_structure
    for mid in reversed(ids):
        row = seen[mid]
        if source.get("test_text_prefix") and not row["text"].startswith(source["test_text_prefix"]):
            continue
        row.update(source_identity=identity, published_at=parse_ui_time(row["original_time_raw"]))
        # Only public, identity-verified usernames permit a deterministic public message link.
        row["url"] = f"https://t.me/{identity['username']}/{mid}" if identity.get("username") else None
        current = normalize_message(source["key"], row)
        old = store.get(source["key"], mid)
        same_attachments=old and same_media_structure(old,current)
        for media in current["media"]:
            prior = next((m for m in old["media"] if m["position"] == media["position"]), None) if same_attachments else None
            force=recheck_media and (not target_message_id or target_message_id==mid)
            if prior and reusable_media(prior,root) and not force:
                media.update(prior)
                media_stats['reused']+=1
            elif not metadata_only and downloads < config["max_media_per_run"]:
                # Earlier scrolled-out media is deferred; no synthetic original is substituted.
                media.update(download_media(page, mid, media, root, config["max_media_mb"]))
                downloads += 1
                media_stats["downloaded" if media["state"] == "DOWNLOADED" else "unavailable_or_deferred"] += 1
            else:
                if prior and reusable_media(prior,root):
                    media.update(prior)
                    current["warnings"].append("MEDIA_NOT_REVALIDATED")
                else:
                    media.update(state="DEFERRED", error="RUN_MEDIA_BUDGET")
                    media_stats["unavailable_or_deferred"] += 1
        result = store.save(current)
        counts[result["action"]] += 1
        stored = store.get(source["key"], mid)
        review_items += int(stored is not None and stored["processing_state"] == "NEEDS_REVIEW")
    detail = {"identity": identity, "catchup_anchor": anchor if coverage == "PARTIAL" else None,
              "oldest_seen_id": ids[0] if ids else None,
              "limit": config["max_messages"], "edit_window": config["overlap_messages"],
              'freshness':{'initial':freshness,'final':final_freshness}}
    if not target_message_id:
        store.checkpoint(source["key"], ids[-1] if ids else None, coverage, len(ids), detail)
    return {"source": source["key"], "coverage": coverage, "scanned": len(ids),
            "review_items": review_items, "media": media_stats,
            'freshness':detail['freshness'], 'observation':'CHANGES_OBSERVED' if counts['created'] or counts['edited'] else 'NO_CHANGES_OBSERVED',
            'freshness_warning':'UI readiness does not certify server history completeness', **counts}


def run_sources(page, store, root, config, args):
    selected = [s for s in config["sources"] if
                (args.source == "all" or s["key"] == args.source) and
                (args.command == "check" or s.get("enabled"))]
    if not selected:
        return {"state": "NO_ENABLED_SOURCE", "sources": []}
    results = []
    for index, source in enumerate(selected):
        try:
            if index:
                from scripts.collector.browser import TELEGRAM_WEB, wait_for_session
                page.goto(TELEGRAM_WEB, wait_until="domcontentloaded", timeout=45000)
                state = wait_for_session(page)
                if state != "AUTHENTICATED":
                    results.append({"source": source["key"], "state": state})
                    continue
            identity = open_source(page, source)
            if args.command == "check":
                results.append({"source": source["key"], "identity": identity,
                                "state": "VERIFIED" if identity["verified"] else "NEEDS_BINDING"})
            elif not identity["verified"]:
                results.append({"source": source["key"], "state": "IDENTITY_UNVERIFIED"})
            else:
                results.append(scan_source(page, store, root, config, source, identity,
                    getattr(args,'recheck_media',False),getattr(args,'message_id',None),getattr(args,'metadata_only',False)))
        except Exception as exc:
            code = str(exc) if str(exc) in ("IDENTITY_MISMATCH", "SOURCE_UNAVAILABLE", "SOURCE_KIND_MISMATCH", "VIEWER_CLOSE_FAILED",'SYNC_UNCONFIRMED','NETWORK_UNAVAILABLE') else type(exc).__name__
            results.append({"source": source["key"], "state": code,
                            "step": getattr(page, "collector_step", "SCAN")})
    store.export(Path(root) / "outbox")
    good = all(r.get("coverage") in ("COMPLETE", "BOUNDED_INITIAL", "TARGETED") or r.get("state") == "VERIFIED" for r in results)
    return {"state": "COMPLETE" if good else "PARTIAL", "sources": results}
