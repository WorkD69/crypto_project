"""One-shot, read-only liquidity research on public Bybit V5 candles."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import requests

from scripts.idea_journal import IdeaJournal
from scripts.liquidity_research import METHOD_URL, RULE_VERSION, daily_context, find_observation
from scripts.mvp_scenario import Candle


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "mvp_02_03"
DB = OUT / "ideas.sqlite3"


def bybit(route: str, **params) -> dict:
    """Same public V5 GET and retCode guard used in the MVP 0.1 client."""
    response = requests.get("https://api.bybit.com/v5/market/" + route,
                            params=params, timeout=20)
    response.raise_for_status()
    data = response.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit {route}: {data.get('retCode')} {data.get('retMsg')}")
    return data


def candles_from_response(data: dict, *, as_of: datetime,
                          interval: timedelta) -> list[Candle]:
    raw = sorted(data["result"]["list"], key=lambda r: int(r[0]))
    result = [Candle(datetime.fromtimestamp(int(r[0]) / 1000, timezone.utc),
                     *map(float, r[1:5])) for r in raw]
    return [c for c in result if c.start + interval <= as_of]


def render_chart(candles: list[Candle], research: dict, daily: dict | None,
                 *, symbol: str, snapshot: datetime, target: Path,
                 category: str = 'linear', interval_label: str = '4H', candle_minutes: int = 240,
                 source_levels: list | None = None, publication: datetime | None = None,
                 cutoff_kind: str = 'publication') -> None:
    fig, ax = plt.subplots(figsize=(14, 7), dpi=145)
    fig.patch.set_facecolor("#f5f7fa")
    ax.set_facecolor("#ffffff")
    width = candle_minutes / 1440 * .66
    for c in candles[-80:]:
        x = mdates.date2num(c.start)
        color = "#178b7b" if c.close >= c.open else "#cb5d65"
        ax.vlines(x, c.low, c.high, color=color, linewidth=1.1, zorder=3)
        ax.add_patch(Rectangle((x-width/2, min(c.open, c.close)), width,
                               max(abs(c.close-c.open), c.close*.000001), color=color, zorder=4))
    zone = research.get("zone")
    if zone:
        zlow, zhigh = zone
        if zlow == zhigh:
            ax.axhline(zlow, color="#8054b0", linewidth=1.8, linestyle="--",
                       label=f"Prior {'low' if research['direction']=='LONG' else 'high'} {zlow:,.1f}")
        else:
            ax.axhspan(zlow, zhigh, color="#8054b0", alpha=.22,
                       label=f"Equal {'lows' if research['direction']=='LONG' else 'highs'} "
                             f"{zlow:,.1f}–{zhigh:,.1f}")
            ax.axhline((zlow+zhigh)/2, color="#8054b0", linewidth=1.2, linestyle="--")
        for stamp in research.get("pivot_starts", []):
            pivot = datetime.fromisoformat(stamp)
            if pivot >= candles[max(0,len(candles)-80)].start:
                ax.axvline(pivot, color="#8054b0", alpha=.28, linewidth=.8)
    for level in source_levels or []:
        ax.axhline(level['level'],color='#8054b0',linestyle='--',linewidth=1.6,
                   label=f"Source text level {level['level']:,.2f} (OHLC checked)")
    ax.axhline(candles[-1].close, color="#263c57", linewidth=1, alpha=.8,
               label=f"Last closed {interval_label} {candles[-1].close:,.2f}")
    context = (f" | 1D 20-bar range {daily['range_low']:,.1f}–{daily['range_high']:,.1f}" if daily else '')
    original = (f" | Original {publication:%Y-%m-%d %H:%M} UTC" if publication else '')
    ax.set_title(f"{symbol} · Bybit {category} · {interval_label} liquidity watch · {RULE_VERSION}\n"
                 f"Snapshot {snapshot:%Y-%m-%d %H:%M} UTC" + context + original,
                 fontsize=13, loc="left", color="#263c57")
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=5, maxticks=10))
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax.xaxis.get_major_locator()))
    ax.set_xlabel(f"UTC time · closed {interval_label} candles")
    ax.set_ylabel("USDT")
    ax.grid(alpha=.16)
    ax.legend(loc="best", fontsize=9)
    footer = "RESEARCH TEST • inferred stops, not observed orders • no entry / SL / TP "
    if publication:
        footer += ("• historical cutoff / observation only" if cutoff_kind=='publication' else
                   "• current analysis cutoff / observation only")
    else:
        footer += "• review on new 4H structure or after 3 days"
    fig.text(.09, .01, footer,
             color="#a03545", fontsize=9, weight="bold")
    fig.tight_layout(rect=(0, .025, 1, 1))
    target.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(target)
    plt.close(fig)


def draft_post(research: dict, daily: dict, *, symbol: str, snapshot: datetime,
               valid_until: datetime) -> str:
    if research["status"] != "WATCHING":
        return (f"ИССЛЕДОВАТЕЛЬСКИЙ ТЕСТ · {symbol} · 4H / 1D\n"
                f"Срез {snapshot:%d.%m.%Y %H:%M} UTC. По правилу прошлых экстремумов "
                "проверяемой области сейчас нет; сделку не предлагаем.\n"
                f"Источник идеи: {METHOD_URL}")
    low, high = research["zone"]
    side = "ниже" if research["direction"] == "LONG" else "выше"
    zone_text = f"{low:,.1f}–{high:,.1f}" if low != high else f"около {low:,.1f}"
    kind = "близкие подтверждённые минимумы" if research["zone_kind"] == "equal_lows" else (
        "близкие подтверждённые максимумы" if research["zone_kind"] == "equal_highs" else
        "предыдущий подтверждённый минимум" if research["direction"] == "LONG" else
        "предыдущий подтверждённый максимум")
    return (f"ИССЛЕДОВАТЕЛЬСКИЙ ТЕСТ · {symbol} · 4H / 1D\n"
            f"Наблюдение: предполагаемая ликвидность {side} цены, {zone_text} USDT. "
            f"На закрытых 4H-свечах: {kind}; стопы там не наблюдаем напрямую.\n"
            f"Контекст 1D: диапазон последних 20 закрытых свечей "
            f"{daily['range_low']:,.1f}–{daily['range_high']:,.1f} USDT. "
            "Это контекст, не независимое подтверждение.\n"
            "ТВХ, SL, TP и R:R не заданы: цена ещё не дала подтверждённый свип "
            "с возвратом в диапазон. Касание области само по себе не означает вход.\n"
            f"Отмена наблюдения: пересмотр при новой 4H-структуре; срок до "
            f"{valid_until:%d.%m.%Y %H:%M} UTC. Срез {snapshot:%d.%m.%Y %H:%M} UTC.\n"
            f"Идея ликвидности: {METHOD_URL}. Числовое правило равенства — наше, не автора.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    args = parser.parse_args()
    symbol = args.symbol.upper()
    if not symbol.isalnum() or not symbol.endswith("USDT"):
        parser.error("Use a Bybit linear USDT symbol")
    four = bybit("kline", category="linear", symbol=symbol, interval="240", limit=201)
    snapshot = datetime.fromtimestamp(four["time"] / 1000, timezone.utc)
    daily = bybit("kline", category="linear", symbol=symbol, interval="D", limit=41)
    instrument = bybit("instruments-info", category="linear", symbol=symbol)
    items = instrument["result"]["list"]
    if not items or items[0].get("status") != "Trading":
        raise RuntimeError("Instrument is not trading")
    four_closed = candles_from_response(four, as_of=snapshot, interval=timedelta(hours=4))
    day_closed = candles_from_response(daily, as_of=snapshot, interval=timedelta(days=1))
    research = find_observation(four_closed, as_of=snapshot, interval=timedelta(hours=4))
    context = daily_context(day_closed, as_of=snapshot)
    valid_until = snapshot + timedelta(days=3)
    OUT.mkdir(parents=True, exist_ok=True)
    artifact_stamp = snapshot.strftime("%Y%m%dT%H%M%S%fZ")
    chart = OUT / f"{symbol.lower()}_liquidity_4h_{artifact_stamp}.png"
    render_chart(four_closed, research, context, symbol=symbol, snapshot=snapshot, target=chart)
    post = draft_post(research, context, symbol=symbol, snapshot=snapshot,
                      valid_until=valid_until)
    draft = OUT / f"telegram_draft_{artifact_stamp}.txt"
    draft.write_text(post, encoding="utf-8")
    payload = {"source": METHOD_URL, "created_at": snapshot.isoformat(),
               "symbol": symbol, "timeframe": "4H", "direction": research["direction"] or "NEUTRAL",
               "idea_type": "observation", "factors": [
                   {"kind": research["zone_kind"], "origin": research.get("pivot_starts", []),
                    "rule_version": RULE_VERSION, "source_concept": METHOD_URL}],
               "entry_zone": research["zone"], "sl": None, "targets": [], "rr": None,
               "valid_until": valid_until.isoformat(), "rule_version": RULE_VERSION,
               "chart_path": str(chart), "market_status": research["status"],
               "daily_context": context, "research": research}
    idea_id = IdeaJournal(DB).create(payload)
    (OUT / "research_result.json").write_text(
        json.dumps({"idea_id": idea_id, "snapshot": payload}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(json.dumps({"idea_id": idea_id, "status": research["status"],
                      "zone": research["zone"], "direction": research["direction"],
                      "last_close": research.get("last_closed_price"),
                      "snapshot_utc": snapshot.isoformat(), "chart": str(chart),
                      "draft": str(draft), "db": str(DB)},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
