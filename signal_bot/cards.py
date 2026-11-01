"""Telegram HTML texts. Author text is always escaped."""
from __future__ import annotations

from html import escape

from signal_bot.tracker import Trade

LIMIT = 4000
EXCERPT = 600
SIDES = {"LONG": ("🟢", "ЛОНГ"), "SHORT": ("🔴", "ШОРТ")}
STATUSES = {"WAITING": "ждёт входа", "ENTERED": "в сделке", "SL": "стоп", "TP": "тейк", "TIMEOUT": "закрыта по времени",
            "MISSED": "ушла без входа", "EXPIRED": "истекла", "NOT_TRACKED": "не отслеживается"}
FLAGS = {
    "CHART_ONLY": "Зона только с графика, в тексте поста её чисел нет",
    "NOT_IN_POST": "Этих чисел нет в тексте поста",
    "CHART_PRICE_DRIFT": "Цена на графике расходится с Bybit больше чем на 3%",
    "LEVEL_FAR_FROM_PRICE": "Часть уровней дальше двух раз от цены, я их отбросил",
    "UNKNOWN_SYMBOL": "Этой монеты нет на Bybit",
    "PRICE_UNAVAILABLE": "Bybit не ответил, уровни не сверены с ценой",
    "SCALE_FIXED": "Модель ошиблась в масштабе цены, код поправил уровни",
    "OWNER_FIX": "Уровни исправлены вручную",
}
BREAK_WORDS = {"LONG": "при пробое уровня", "SHORT": "при потере уровня"}


def _price(value: float | None) -> str:
    if value is None:
        return "не указан"
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _r(value: float | None) -> str:
    return f"{value or 0:+.2f}R"


def _short(text: str, size: int) -> str:
    return escape(text if len(text) <= size else text[:size].rstrip() + "…")


def _bounds(low: float | None, high: float | None) -> str:
    if low is None:
        return "границы не прочитаны"
    return _price(low) if low == high else f"{_price(low)}–{_price(high)}"


def _zone_name(zone: dict) -> str:
    return f"<b>{escape(zone['symbol'])} {SIDES.get(zone['side'], ('', 'без направления'))[1]}</b>"


def _distance(zone: dict) -> str:
    price, low, high = zone["price"], zone["zone_low"], zone["zone_high"]
    if price is None:
        return "нет данных"
    if low is None:
        return _price(price)
    if low <= price <= high:
        return f"{_price(price)}, цена в зоне"
    edge = low if price < low else high
    return f"{_price(price)}, до зоны {(edge - price) / price * 100:+.2f}%"


def _link(url: str | None) -> list[str]:
    return [f'<a href="{escape(url, quote=True)}">Оригинал поста</a>'] if url else []


def render_zone(zone: dict) -> str:
    mark, word = SIDES.get(zone["side"], ("⚪️", "НАПРАВЛЕНИЕ НЕ УКАЗАНО"))
    frame = f" · {escape(zone['timeframe'])}" if zone["timeframe"] else ""
    bounds = _bounds(zone["zone_low"], zone["zone_high"])
    if zone["trigger"] == "break" and zone["zone_low"] is not None:
        bounds = f"{BREAK_WORDS.get(zone['side'], 'при пробое уровня')} {bounds}"
    stop = "стоп не указан" if zone["stop"] is None else _price(zone["stop"])
    targets = ", ".join(_price(value) for value in zone["targets"]) or "не указаны"
    lines = [f"{mark} <b>{word} {escape(zone['symbol'])}</b>{frame}", f"Автор: {escape(zone['author'])}", "",
             f"Вход: {bounds}", f"Стоп: {stop}", f"Цели: {targets}", f"Цена: {_distance(zone)}",
             f"Уверенность: {zone['confidence'] * 100:.0f}%"]
    notes = [FLAGS[code] for code in zone["flags"] if code in FLAGS]
    if "CHECK_BY_EYE" in zone["flags"]:
        lines += ["", "⚠️ <b>Проверь глазами</b>"] + [f"• {note}" for note in notes]
    elif notes:
        lines += [""] + [f"⚠️ {note}" for note in notes]
    return "\n".join(lines + ["", f"<blockquote>{_short(zone['evidence'], EXCERPT)}</blockquote>"] + _link(zone["url"]))


def _zone_levels(levels: dict) -> str:
    stop = "стоп не указан" if levels["stop"] is None else f"стоп {_price(levels['stop'])}"
    targets = ", ".join(_price(value) for value in levels["targets"]) or "не указаны"
    return f"{_bounds(levels['zone_low'], levels['zone_high'])}, {stop}, цели {targets}"


def render_zone_event(zone: dict, event: dict, url: str | None) -> str:
    name, bounds = _zone_name(zone), _bounds(zone["zone_low"], zone["zone_high"])
    if event["kind"] == "cancel":
        lines = [f"❌ Автор отменил зону {name}: {bounds}"]
    elif event["kind"] == "result":
        lines = [f"🏁 Автор сообщил итог по зоне {name}: {bounds}"]
    else:
        lines = [f"✏️ Автор обновил зону {name}", f"Было: {_zone_levels(event['before'])}",
                 f"Стало: {_zone_levels(zone)}"]
    return "\n".join(lines + _link(url))


def render_unparsed(post: dict, images: int) -> str:
    text = post["edited_text"] or post["text"]
    body = f"<blockquote>{_short(text, EXCERPT)}</blockquote>" if text else f"Без подписи, картинок: {images}"
    return "\n".join(["⏳ <b>Пост без разбора</b>: модель сейчас не прочитала его, попробую позже",
                      f"Автор: {escape(post['author'])}", "", body] + _link(post["url"]))


def render_unclear(zone, url: str | None) -> str:
    """The author closes something, and the code cannot prove which zone it is."""
    return "\n".join([f"⚠️ Автор отменяет или закрывает зону по <b>{escape(zone.symbol)}</b>, "
                      "но неясно какую. Зоны остались открытыми, проверь пост."] + _link(url))


def render_zone_edit(zones: list[dict], text: str) -> str:
    names = ", ".join(sorted({escape(zone["symbol"]) for zone in zones}))
    return (f"✏️ Автор изменил пост по <b>{names}</b>. Уровни зон остались прежними.\n\n"
            f"<blockquote>{_short(text, EXCERPT)}</blockquote>")


def render_zone_delete(zones: list[dict]) -> str:
    names = ", ".join(sorted({escape(zone["symbol"]) for zone in zones}))
    return f"🗑 Автор удалил пост по <b>{names}</b>. Зоны остаются в работе."


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


def _zone_line(zone: dict) -> str:
    bounds = _bounds(zone["zone_low"], zone["zone_high"])
    return f"• {_zone_name(zone)} {bounds}, {escape(zone['author'])}"


def render_digest(opinions: list[dict], ideas: list[dict], zones=(), unparsed: int = 0) -> str:
    if not opinions and not ideas and not zones and not unparsed:
        return "<b>Сводка за день</b>\nСегодня ничего нового."
    lines = ["<b>Сводка за день</b>"]
    if unparsed:
        lines += ["", f"⏳ Без разбора: {unparsed}"]
    if zones:
        lines += ["", "<b>Зоны за день</b>"] + [_zone_line(zone) for zone in zones]
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


def render_open(ideas: list[dict], zones=()) -> str:
    if not ideas and not zones:
        return "Открытых идей нет."
    lines = []
    if zones:
        lines += ["<b>Открытые зоны</b>"] + [_zone_line(zone) for zone in zones]
    if ideas:
        lines.append("<b>Открытые идеи</b>")
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
