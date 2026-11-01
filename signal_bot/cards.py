"""Telegram HTML texts. Author text is always escaped."""
from __future__ import annotations

from html import escape

from signal_bot.tracker import Trade

LIMIT = 4000
EXCERPT = 600
SIDES = {"LONG": ("🟢", "ЛОНГ"), "SHORT": ("🔴", "ШОРТ")}
WARNINGS = {
    "LEVEL_FAR_FROM_PRICE": "Часть уровней слишком далеко от цены, я их отбросил",
    "SIDE_FROM_LEVELS": "Направление определено по уровням, в тексте его нет",
    "PRICE_UNAVAILABLE": "Bybit не ответил, уровни не сверены с ценой",
    "BAD_LEVEL_ORDER": "Уровни не сходятся с направлением",
}
STATUSES = {"WAITING": "ждёт входа", "ENTERED": "в сделке", "SL": "стоп", "TP": "тейк", "TIMEOUT": "закрыта по времени",
            "MISSED": "ушла без входа", "EXPIRED": "истекла", "NOT_TRACKED": "не отслеживается"}


def _price(value: float | None) -> str:
    if value is None:
        return "не указан"
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _r(value: float | None) -> str:
    return f"{value or 0:+.2f}R"


def _short(text: str, size: int) -> str:
    return escape(text if len(text) <= size else text[:size].rstrip() + "…")


def _rr(idea: dict) -> float | None:
    if idea["entry_low"] is None or idea["stop"] is None or not idea["targets"]:
        return None
    entry = (idea["entry_low"] + idea["entry_high"]) / 2
    risk = abs(entry - idea["stop"])
    return abs(idea["targets"][0] - entry) / risk if risk else None


def _title(idea: dict) -> str:
    mark, word = SIDES.get(idea["side"], ("⚪️", "НАПРАВЛЕНИЕ НЕ УКАЗАНО"))
    return f"{mark} <b>{word} {escape(idea['symbol'])}</b>"


def _entry(idea: dict) -> str:
    low, high = idea["entry_low"], idea["entry_high"]
    if idea["entry_assumed"]:
        return f"по рынку, около {_price(low)}"
    return _price(low) if low == high else f"{_price(low)}–{_price(high)}"


def render_card(idea: dict) -> str:
    rr = _rr(idea)
    targets = ", ".join(_price(value) for value in idea["targets"]) or "не указаны"
    lines = [_title(idea), f"Автор: {escape(idea['author'])}", "", f"Вход: {_entry(idea)}",
             f"Стоп: {_price(idea['stop'])}", f"Тейки: {targets}", f"RR: {rr:.2f}" if rr else "RR: нет данных"]
    if idea["price"] is not None:
        lines.append(f"Цена сейчас: {_price(idea['price'])}")
    notes = [WARNINGS[code] for code in idea["warnings"] if code in WARNINGS]
    if idea["status"] == "NOT_TRACKED":
        notes.append("Не отслеживается: нужны направление, вход и стоп")
    if notes:
        lines += [""] + [f"⚠️ {note}" for note in notes]
    lines += ["", f"<blockquote>{_short(idea['text'], EXCERPT)}</blockquote>"]
    if idea["url"]:
        lines.append(f'<a href="{escape(idea["url"], quote=True)}">Оригинал поста</a>')
    return "\n".join(lines)


def render_event(idea: dict, trade: Trade) -> str:
    name = f"<b>{escape(idea['symbol'])}</b>"
    if trade.status == "ENTERED":
        return f"✅ {name}: вход сработал по {_price(trade.fill_price)}"
    if trade.status == "TP":
        return f"🎯 {name}: тейк, {_r(trade.result_r)}"
    if trade.status == "SL":
        extra = "\nСтоп и тейк попали в одной свече, считаю как стоп." if trade.ambiguous else ""
        return f"🛑 {name}: стоп, {_r(trade.result_r)}{extra}"
    if trade.status == "TIMEOUT":
        return f"⏱ {name}: 14 дней без стопа и тейка, закрыта по цене, {_r(trade.result_r)}"
    if trade.status == "MISSED":
        return f"💨 {name}: цена дошла до тейка без входа"
    return f"⌛️ {name}: за 14 дней вход не сработал"


def render_edit(idea: dict, text: str) -> str:
    return (f"✏️ Автор изменил пост по <b>{escape(idea['symbol'])}</b>. Уровни идеи остались прежними.\n\n"
            f"<blockquote>{_short(text, EXCERPT)}</blockquote>")


def render_delete(idea: dict) -> str:
    return (f"🗑 Автор удалил пост по <b>{escape(idea['symbol'])}</b>. "
            f"Идею продолжаю считать: сейчас она {STATUSES[idea['status']]}.")


def render_digest(opinions: list[dict], ideas: list[dict]) -> str:
    if not opinions and not ideas:
        return "<b>Сводка за день</b>\nСегодня ничего нового."
    lines = ["<b>Сводка за день</b>"]
    authors: dict[str, list[str]] = {}
    for post in opinions:
        authors.setdefault(post["author"], []).append(post["text"])
    for author, texts in authors.items():
        lines += ["", f"<b>{escape(author)}</b>"] + [f"• {_short(text, 300)}" for text in texts]
    if ideas:
        lines += ["", "<b>Идеи за день</b>"]
        for idea in ideas:
            word = SIDES.get(idea["side"], ("", "без направления"))[1]
            lines.append(f"• {escape(idea['symbol'])} {word}, {escape(idea['author'])}: {STATUSES[idea['status']]}")
    return "\n".join(lines)


def render_stats(rows: list[dict]) -> str:
    if not rows:
        return "Пока нет идей для статистики."
    lines = ["<b>Статистика по авторам</b>", "Результат на бумаге, с комиссией 0,055% на сторону.", ""]
    for row in rows:
        parts = [f"идей {row['ideas']}"]
        if row["closed"]:
            winrate = row["wins"] / row["closed"] * 100
            parts += [f"закрыто {row['closed']}", f"побед {winrate:.0f}%", f"итог {_r(row['total_r'])}",
                      f"по взятым {_r(row['taken_r'])}"]
        else:
            parts.append("закрытых пока нет")
        if row["deleted"]:
            parts.append(f"удалено {row['deleted']}")
        lines.append(f"<b>{escape(row['author'])}</b>: " + ", ".join(parts))
    return "\n".join(lines)


def render_open(ideas: list[dict]) -> str:
    if not ideas:
        return "Открытых идей нет."
    lines = ["<b>Открытые идеи</b>"]
    for idea in ideas:
        word = SIDES.get(idea["side"], ("", ""))[1]
        lines.append(f"• {escape(idea['symbol'])} {word}, {escape(idea['author'])}: {STATUSES[idea['status']]}")
    return "\n".join(lines)


def chunks(text: str) -> list[str]:
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > LIMIT:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:LIMIT])
            line = line[LIMIT:]
        if current and len(current) + len(line) + 1 > LIMIT:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts
