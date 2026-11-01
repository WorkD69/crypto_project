"""Owner corrections of a zone, written as one short message. Numbers are parsed by code, not by the model."""
from __future__ import annotations

import re

from signal_bot.extract import normalize_symbol

REJECT = object()
NO_ZONE = {"нет", "нет зоны", "зоны нет"}
NUMBER = r"\d+(?:[.,]\d+)?(?:\s*[kк])?"
STOP_RE = re.compile(rf"стоп\s+(нет|{NUMBER})")
TARGETS_RE = re.compile(rf"(?:цел[ьи]|тейки?)\s+({NUMBER}(?:[\s,;–—-]+{NUMBER})*)")
RANGE_RE = re.compile(rf"({NUMBER})\s*[–—-]\s*({NUMBER})")
LEVEL_RE = re.compile(rf"(?:уровень|зона|вход)\s+({NUMBER})")
ANY_NUMBER_RE = re.compile(NUMBER)
SIDE_RE = re.compile(r"\b(лонг|long|шорт|short)\b")
TICKER_RE = re.compile(r"\b[a-z][a-z0-9]+\b")
SIDES = {"лонг": "LONG", "long": "LONG", "шорт": "SHORT", "short": "SHORT"}
FIX_HELP = ("Напиши, как правильно, одним сообщением. Пиши только то, что неверно:\n"
            "шорт 82000-84000 стоп 85500 цели 78000 76000\n"
            "стоп 85500\n"
            "ETH лонг 2380-2460\n"
            "уровень 140\n"
            "нет: зоны здесь нет совсем\n"
            "отмена: ничего не менять")


def _number(raw: str, thousands: bool = False) -> float:
    text = raw.strip()
    if text[-1] in "kк":
        thousands, text = True, text[:-1].strip()
    value = float(text.replace(",", "."))
    return value * 1000 if thousands else value


def _bounds(text: str) -> tuple[float, float] | None:
    match = RANGE_RE.search(text)
    if match:
        first, second = match.groups()
        shared = second.strip()[-1] in "kк"
        low, high = _number(first, shared), _number(second)
        return min(low, high), max(low, high)
    match = LEVEL_RE.search(text)
    if match is None:
        return None
    level = _number(match.group(1))
    return level, level


def parse_fix(text: str, symbols: set[str]) -> dict | object | None:
    """Fields to change, REJECT for a zone that does not exist, None for words that say nothing."""
    text = text.strip().lower()
    if text in NO_ZONE:
        return REJECT
    fields: dict = {}
    stop = STOP_RE.search(text)
    if stop:
        fields["stop"] = None if stop.group(1) == "нет" else _number(stop.group(1))
        text = text.replace(stop.group(0), " ")
    targets = TARGETS_RE.search(text)
    if targets:
        fields["targets"] = [_number(item) for item in ANY_NUMBER_RE.findall(targets.group(1))]
        text = text.replace(targets.group(0), " ")
    bounds = _bounds(text)
    if bounds:
        fields["zone_low"], fields["zone_high"] = bounds
    side = SIDE_RE.search(text)
    if side:
        fields["side"] = SIDES[side.group(1)]
    ticker = next((word for word in TICKER_RE.findall(text) if word not in SIDES), None)
    if ticker:
        symbol = normalize_symbol(ticker, symbols)
        if symbol is None:
            return None
        fields["symbol"] = symbol
    return fields or None
