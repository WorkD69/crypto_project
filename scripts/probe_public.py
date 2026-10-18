"""Small, credential-free Telegraph and Bybit probes. Output contains summaries only."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone


def fetch(url: str) -> dict:
    started = time.perf_counter()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "crypto-feasibility-spike/0.1"})
        with urllib.request.urlopen(req, timeout=12) as response:
            body = response.read(2_000_000)
            return {"http": response.status, "ms": round((time.perf_counter() - started) * 1000),
                    "bytes": len(body), "headers": {k: response.headers.get(k) for k in
                    ("X-Bapi-Limit", "X-Bapi-Limit-Status", "X-Bapi-Limit-Reset-Timestamp") if response.headers.get(k)},
                    "json": json.loads(body) if body.lstrip().startswith(b"{") else None}
    except urllib.error.HTTPError as exc:
        return {"http": exc.code, "ms": round((time.perf_counter() - started) * 1000),
                "error": exc.read(500).decode("utf-8", "replace")}
    except Exception as exc:
        return {"error_type": type(exc).__name__, "error": str(exc),
                "ms": round((time.perf_counter() - started) * 1000)}


def walk(nodes, path=()):
    for index, node in enumerate(nodes):
        here = path + (index,)
        if isinstance(node, str):
            yield {"path": here, "tag": "text", "length": len(node)}
        elif isinstance(node, dict):
            attrs = node.get("attrs", {})
            yield {"path": here, "tag": node.get("tag"), "src": attrs.get("src"),
                   "child_count": len(node.get("children", []))}
            yield from walk(node.get("children", []), here)


def telegraph():
    path = "Likvidnost-poisk-tochek-vhoda-07-27-2"
    page = fetch("https://telegra.ph/" + path)
    api = fetch("https://api.telegra.ph/getPage/" + path + "?return_content=true")
    bad = fetch("https://api.telegra.ph/getPage/This-page-does-not-exist-feasibility-10-12?return_content=true")
    data = api.get("json") or {}
    result = data.get("result") or {}
    nodes = list(walk(result.get("content") or []))
    tags = Counter(n.get("tag") for n in nodes)
    images = [n for n in nodes if n.get("tag") == "img"]
    figures = [n for n in nodes if n.get("tag") == "figure"]
    def text_of(node):
        if isinstance(node, str):
            return node
        return "".join(text_of(child) for child in node.get("children", []))
    figure_pairs = []
    for top in result.get("content") or []:
        if isinstance(top, dict) and top.get("tag") == "figure":
            children = top.get("children") or []
            figure_pairs.append({"image": any(isinstance(x, dict) and x.get("tag") == "img" for x in children),
                                 "caption": any(isinstance(x, dict) and x.get("tag") == "figcaption" for x in children)})
    print(json.dumps({"check": "telegraph", "page_http": page.get("http"),
                      "page_error": page.get("error_type") or page.get("error"),
                      "api_http": api.get("http"), "api_ms": api.get("ms"),
                      "api_ok": data.get("ok"), "api_error": data.get("error"),
                      "title": result.get("title"), "author": result.get("author_name"),
                      "top_level_nodes": len(result.get("content") or []), "tags": tags,
                      "image_count": len(images), "figure_count": len(figures),
                      "images_with_src": sum(bool(n.get("src")) for n in images),
                      "figures_with_image_and_caption": sum(x["image"] and x["caption"] for x in figure_pairs),
                      "text_chars": len(" ".join(text_of(n) for n in result.get("content") or [])),
                      "ordered_structure": bool(nodes),
                      "bad_path_http": bad.get("http"),
                      "bad_path_response": bad.get("json"),
                      "bad_path_error": bad.get("error_type") or bad.get("error")}, ensure_ascii=True))


def bybit(base: str):
    endpoint = base.rstrip("/") + "/v5/market/"
    tests = [("time", "time", {}), ("instruments", "instruments-info", {"category": "linear", "limit": "1000"})]
    for symbol in ("BTCUSDT", "ETHUSDT"):
        for interval in ("15", "60", "240"):
            tests.append((f"{symbol}-{interval}", "kline", {"category": "linear", "symbol": symbol,
                                                                "interval": interval, "limit": "5"}))
    tests.extend([("btc_instrument", "instruments-info", {"category": "linear", "symbol": "BTCUSDT"}),
                  ("funding", "funding/history", {"category": "linear", "symbol": "BTCUSDT", "limit": "2"}),
                  ("oi", "open-interest", {"category": "linear", "symbol": "BTCUSDT", "intervalTime": "1h", "limit": "2"})])
    for year in (2020, 2022, 2024):
        start = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        tests.append((f"btc_history_{year}", "kline", {"category": "linear", "symbol": "BTCUSDT",
                                                        "interval": "240", "start": str(start),
                                                        "end": str(start + 7 * 86400_000), "limit": "2"}))
    for label, route, params in tests:
        url = endpoint + route + ("?" + urllib.parse.urlencode(params) if params else "")
        response = fetch(url)
        data = response.get("json") or {}
        result = data.get("result") or {}
        record = {"check": "bybit", "base": base, "case": label, "http": response.get("http"),
                  "ms": response.get("ms"), "bytes": response.get("bytes"),
                  "retCode": data.get("retCode"), "retMsg": data.get("retMsg"),
                  "headers": response.get("headers"),
                  "error_type": response.get("error_type"), "error": response.get("error")}
        if route == "kline" and isinstance(result.get("list"), list):
            candles = result["list"]
            starts = [int(c[0]) for c in candles]
            duration = int(params["interval"]) * 60_000
            now = int(time.time() * 1000)
            record.update({"count": len(candles), "starts": starts,
                           "descending": starts == sorted(starts, reverse=True),
                           "latest_closed": bool(starts and starts[0] + duration <= now),
                           "oldest_utc": datetime.fromtimestamp(min(starts) / 1000, timezone.utc).isoformat() if starts else None})
        elif route == "instruments-info":
            items = result.get("list") or []
            record.update({"count": len(items), "next_cursor": bool(result.get("nextPageCursor")),
                           "trading_count": sum(i.get("status") == "Trading" for i in items)})
            if label == "btc_instrument" and items:
                item = items[0]
                record.update({"tick_size": item.get("priceFilter", {}).get("tickSize"),
                               "min_order_qty": item.get("lotSizeFilter", {}).get("minOrderQty"),
                               "max_leverage": item.get("leverageFilter", {}).get("maxLeverage"),
                               "funding_interval": item.get("fundingInterval")})
        elif route in ("funding/history", "open-interest"):
            record["count"] = len(result.get("list") or [])
        print(json.dumps(record, ensure_ascii=True))


if __name__ == "__main__":
    telegraph()
    bybit("https://api.bybit.com")
