"""Create a small original Bybit chart and test the installed local vision model."""
import base64
import json
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import requests


OUT = Path(__file__).resolve().parents[1] / "artifacts"
OUT.mkdir(exist_ok=True)


def make_chart() -> Path:
    data = requests.get("https://api.bybit.com/v5/market/kline", params={
        "category": "linear", "symbol": "BTCUSDT", "interval": "60", "limit": 33}, timeout=15).json()
    if data["retCode"] != 0:
        raise RuntimeError(f"Bybit retCode {data['retCode']}: {data['retMsg']}")
    raw = sorted(data["result"]["list"], key=lambda row: int(row[0]))[:-1]
    if len(raw) < 20:
        raise RuntimeError("Too few closed candles")
    lows = [float(x[3]) for x in raw]
    highs = [float(x[2]) for x in raw]
    zone_low = min(lows[-20:])
    zone_high = zone_low + (max(highs[-20:]) - zone_low) * .10
    fig, ax = plt.subplots(figsize=(12, 6), dpi=100)
    for i, row in enumerate(raw):
        high, low, close, open_ = map(float, (row[2], row[3], row[4], row[1]))
        color = "#16a085" if close >= open_ else "#d35454"
        ax.vlines(i, low, high, color=color, linewidth=1.2)
        ax.add_patch(plt.Rectangle((i-.3, min(open_, close)), .6, max(abs(close-open_), .01), color=color))
    ax.axhspan(zone_low, zone_high, color="#2b75d6", alpha=.22)
    ax.text(0.02, .08, f"TEST ZONE {zone_low:.1f}–{zone_high:.1f} USDT", transform=ax.transAxes,
            color="#164f9b", fontsize=13, weight="bold")
    ax.set_title("BYBIT BTCUSDT | LINEAR | 1H | CLOSED CANDLES | TEST CHART", fontsize=16)
    ax.set_xlabel("Closed candle index (oldest to newest)")
    ax.set_ylabel("Price (USDT)")
    ax.grid(alpha=.15)
    fig.tight_layout()
    target = OUT / "btc_test_chart.png"
    fig.savefig(target)
    plt.close(fig)
    print(json.dumps({"chart": str(target), "candles": len(raw), "zone": [round(zone_low, 1), round(zone_high, 1)],
                      "size_bytes": target.stat().st_size}))
    return target


def vision(chart: Path) -> None:
    image = base64.b64encode(chart.read_bytes()).decode("ascii")
    started = time.perf_counter()
    response = requests.post("http://127.0.0.1:11434/api/chat", json={
        "model": "hf.co/unsloth/Qwen3.5-4B-GGUF:Q4_K_M", "stream": False, "think": False,
        "options": {"num_ctx": 8192, "num_predict": 350, "temperature": 0},
        "messages": [{"role": "user", "content": "Read the chart. State asset, timeframe, any labeled zone and its approximate price boundaries. Say when a label is unreadable. Do not infer a trade signal.", "images": [image]}]
    }, timeout=180)
    response.raise_for_status()
    data = response.json()
    print(json.dumps({"vision_model": data.get("model"), "seconds": round(time.perf_counter()-started, 2),
                      "response": data.get("message", {}).get("content", "")[:1500],
                      "prompt_eval_count": data.get("prompt_eval_count"), "eval_count": data.get("eval_count")}, ensure_ascii=True))


if __name__ == "__main__":
    vision(make_chart())
