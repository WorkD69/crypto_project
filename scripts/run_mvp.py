"""One-shot BTCUSDT research demo from public Bybit V5 data; never places orders."""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import requests

from mvp_scenario import Candle, analyze


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts"
SYMBOL = "BTCUSDT"
INTERVAL = "60"
RULE = "MVP-DEMO-0.1"


def bybit(route: str, **params):
    started = time.perf_counter()
    response = requests.get("https://api.bybit.com/v5/market/" + route,
                            params=params, timeout=15)
    response.raise_for_status()
    data = response.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit {route}: retCode={data.get('retCode')} {data.get('retMsg')}")
    return data, round(time.perf_counter() - started, 3)


def render(candles: list[Candle], result: dict, snapshot: datetime, path: Path):
    fig, ax = plt.subplots(figsize=(12, 7), dpi=130)
    fig.patch.set_facecolor("#f7f9fc")
    ax.set_facecolor("#f7f9fc")
    for i, c in enumerate(candles):
        color = "#179b82" if c.close >= c.open else "#da646b"
        ax.vlines(i, c.low, c.high, color=color, linewidth=1)
        ax.add_patch(plt.Rectangle((i - .32, min(c.open, c.close)), .64,
                                   max(abs(c.close - c.open), .01), color=color))
    if result.get("fib_zone"):
        ax.axhspan(*result["fib_zone"], color="#a16add", alpha=.16, label="Fibonacci 0.5–0.618")
    if result.get("test_zone"):
        ax.axhspan(*result["test_zone"], color="#2376d2", alpha=.16, label="Тестовая область свечи")
    if result.get("overlap"):
        ax.axhspan(*result["overlap"], color="#e1a72a", alpha=.28, label="Пересечение зон")
    if result["status"] == "CANDIDATE":
        for key, color in (("entry", "#1f4d95"), ("sl", "#c83542"), ("tp", "#11815b")):
            ax.axhline(result[key], color=color, linestyle="--", linewidth=1.4,
                       label=f"{key.upper()} {result[key]:,.1f}")
    status_label = "КАНДИДАТ" if result["status"] == "CANDIDATE" else "ОТКЛОНЕНО"
    ax.set_title(f"{SYMBOL} · Bybit linear · 1H · {RULE}\n"
                 f"Снимок {snapshot:%Y-%m-%d %H:%M:%S} UTC | {status_label}", fontsize=12)
    ax.set_xlabel("Закрытые свечи: от старых к новым")
    ax.set_ylabel("USDT")
    ax.grid(alpha=.14)
    ax.legend(loc="best", fontsize=9)
    ax.text(.01, .02, "ТЕСТОВЫЙ СЦЕНАРИЙ, НЕ ТОРГОВЫЙ СИГНАЛ", transform=ax.transAxes,
            color="#a02028", fontsize=10, weight="bold")
    if result["status"] == "REJECTED":
        ax.text(.01, .95, "Вход отклонён: условия не выполнены", transform=ax.transAxes,
                color="#a02028", fontsize=10, weight="bold", va="top")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def main():
    OUT.mkdir(exist_ok=True)
    market, market_s = bybit("kline", category="linear", symbol=SYMBOL,
                             interval=INTERVAL, limit=81)
    instrument, instrument_s = bybit("instruments-info", category="linear", symbol=SYMBOL)
    snapshot = datetime.fromtimestamp(market["time"] / 1000, timezone.utc)
    interval = timedelta(hours=1)
    raw = sorted(market["result"]["list"], key=lambda row: int(row[0]))
    candles = [Candle(datetime.fromtimestamp(int(r[0]) / 1000, timezone.utc),
                      float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in raw]
    closed = [c for c in candles if c.start + interval <= snapshot]
    if len(closed) < 56:
        raise RuntimeError("Fewer than 56 closed candles")
    # Anchors are explicit candle IDs in this immutable snapshot, picked from the
    # first 48 of the last 56 closed candles. The final 8 form the test-zone window.
    selected = closed[-56:]
    anchor_pool = selected[:48]
    low_anchor = min(range(48), key=lambda i: anchor_pool[i].low)
    high_anchor = max(range(48), key=lambda i: anchor_pool[i].high)
    # The separate test area is fixed by the widest recent candle, without
    # looking at Fibonacci levels. Its index and timestamp are recorded below.
    zone_candle_index = max(range(48, 56),
                            key=lambda i: selected[i].high - selected[i].low)
    item = instrument["result"]["list"][0]
    result = analyze(selected, as_of=snapshot, interval=interval,
                     low_anchor=low_anchor, high_anchor=high_anchor,
                     zone_candle_index=zone_candle_index, min_rr=1.5,
                     tick_size=float(item["priceFilter"]["tickSize"]))
    payload = {"rule_version": RULE, "symbol": SYMBOL, "timeframe": "1H",
               "snapshot_utc": snapshot.isoformat(), "closed_candles": len(closed),
               "excluded_unclosed": len(candles) - len(closed),
               "anchor_low": {"index": low_anchor, "start": selected[low_anchor].start.isoformat(),
                              "price": selected[low_anchor].low},
               "anchor_high": {"index": high_anchor, "start": selected[high_anchor].start.isoformat(),
                               "price": selected[high_anchor].high},
               "test_zone_candle": {"index": zone_candle_index,
                                    "start": selected[zone_candle_index].start.isoformat()},
               "tick_size": item["priceFilter"]["tickSize"],
               "network_seconds": {"kline": market_s, "instrument": instrument_s},
               "scenario": result}
    path = OUT / "mvp_btcusdt_1h.png"
    render(selected, result, snapshot, path)
    print(json.dumps({**payload, "chart_path": str(path), "chart_bytes": path.stat().st_size},
                     ensure_ascii=True, indent=2))
    with (OUT / "mvp_result.json").open("w", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, indent=2)
    if result["status"] == "CANDIDATE":
        body = (f"BTCUSDT · 1H · {snapshot:%d.%m.%Y %H:%M} UTC\n"
                f"Тестовая зона и Fibonacci 0.5–0.618 пересекаются. "
                f"Направление: {result['direction']}. "
                f"Условные уровни: entry {result['entry']:.1f}, SL {result['sl']:.1f}, "
                f"TP {result['tp']:.1f}; R:R {result['rr']:.2f}.\n")
    else:
        reasons = {"price no longer near entry": "цена ушла от области входа",
                   "risk/reward below threshold": "отношение потенциальной прибыли к риску ниже порога",
                   "no zone overlap": "ценовые области не пересекаются",
                   "invalid level order": "некорректный порядок уровней"}
        body = (f"BTCUSDT · 1H · {snapshot:%d.%m.%Y %H:%M} UTC\n"
                f"Тестовые правила не дали допустимого входа: "
                f"{reasons.get(result['reason'], 'условия не выполнены')}. "
                "Entry, SL и TP не назначены.\n")
    (OUT / "telegram_draft.txt").write_text(
        "ТЕСТОВЫЙ СЦЕНАРИЙ, НЕ ТОРГОВЫЙ СИГНАЛ\n" + body +
        "Основа: закрытые свечи Bybit V5, зафиксированные опорные точки и демонстрационные правила. "
        "Перед любым использованием нужна новая проверка рынка.\n", encoding="utf-8")


if __name__ == "__main__":
    main()
