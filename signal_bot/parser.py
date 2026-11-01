"""Deterministic post parser. Prices are text until checked against the market."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace

ALIASES = ((r"биткоин\w*|биткойн\w*|битк[аеу]\b", "BTC"), (r"[эе]фир\w*", "ETH"), (r"солан\w*", "SOL"))
NOT_COINS = {"TP", "SL", "RR", "FVG", "ATR", "OB", "ETF", "USD", "USDT", "CME", "OI", "LTF", "HTF"}
SIDES = ((r"лонг\w*|long\w*|buy", "LONG"), (r"шорт\w*|short\w*|sell", "SHORT"))
LABELS = (
    ("entry", r"точк\w+ входа|вход\w*|твх|entry|лимитк\w*"),
    ("stop", r"стоп(?:[- ]?лосс)?|stop(?:[- ]?loss)?|sl"),
    ("target", r"тейк(?:[- ]?профит)?|take(?:[- ]?profit)?|тп\s*\d*|tp\s*\d*|цел[иья]|target\w*"),
)
LABEL_RE = re.compile("|".join(f"(?<!\\w)(?P<{name}>{body})(?!\\w)" for name, body in LABELS), re.I)
NUMBER_RE = re.compile(
    r"(?<![\w.,])(?:\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)*)(?![\w%]|\s*%)")
LIST_MARKER_RE = re.compile(r"(?:^|(?<=\s))\d{1,2}[).](?=\s)")
GROUPED_RE = re.compile(r"\d{1,3}(?:[.,]\d{3})+")
MAX_PRICE_RATIO = 2.0


@dataclass
class ParsedPost:
    symbol: str | None = None
    side: str | None = None
    raw_entry: list[str] = field(default_factory=list)
    raw_stop: list[str] = field(default_factory=list)
    raw_targets: list[str] = field(default_factory=list)
    entry_low: float | None = None
    entry_high: float | None = None
    stop: float | None = None
    targets: list[float] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_idea(self) -> bool:
        return bool(self.symbol and (self.raw_entry or self.raw_stop or self.raw_targets))


def _find_symbol(text: str, symbols: set[str]) -> str | None:
    def known(base: str) -> str | None:
        base = base.upper()
        if base in NOT_COINS or base.isdigit():
            return None
        for prefix in ("", "1000", "10000"):
            if f"{prefix}{base}USDT" in symbols:
                return f"{prefix}{base}USDT"
        return None

    patterns = (r"[#$]([A-Za-z0-9]{2,20})", r"(?<![A-Za-z0-9])([A-Za-z0-9]{2,20}?)[/\-]?USDT(?:\.P)?(?![A-Za-z0-9])",
                r"(?<![A-Za-z0-9])([A-Z0-9]{2,15})(?![A-Za-z0-9])")
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I if "USDT" in pattern else 0):
            if found := known(match.group(1)):
                return found
    for pattern, base in ALIASES:
        if re.search(rf"(?<!\w)(?:{pattern})", text, re.I) and (found := known(base)):
            return found
    return None


def _find_side(text: str) -> str | None:
    found = {side for pattern, side in SIDES if re.search(rf"(?<!\w)(?:{pattern})(?!\w)", text, re.I)}
    return found.pop() if len(found) == 1 else None


def _segment(text: str, start: int, stop: int) -> str:
    """Label's own line, plus a value line or a numbered list right below it."""
    lines = text[start:stop].split("\n")
    taken = [lines[0]]
    for line in lines[1:]:
        if LIST_MARKER_RE.match(line.strip()) or not NUMBER_RE.search(" ".join(taken)):
            taken.append(line)
        else:
            break
    return "\n".join(taken)


def _numbers(segment: str) -> list[str]:
    return NUMBER_RE.findall(LIST_MARKER_RE.sub(" ", segment))


def parse_post(text: str, symbols: set[str]) -> ParsedPost:
    parsed = ParsedPost(symbol=_find_symbol(text, symbols), side=_find_side(text))
    matches = list(LABEL_RE.finditer(text))
    for index, match in enumerate(matches):
        stop = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        values = _numbers(_segment(text, match.end(), stop))
        if match.lastgroup == "entry":
            parsed.raw_entry += values
        elif match.lastgroup == "stop":
            parsed.raw_stop += values
        else:
            parsed.raw_targets += values
    return parsed


def resolve_number(raw: str, price: float | None) -> float | None:
    """Pick the reading of an ambiguous number that is closest to the market."""
    text = re.sub(r"[   ]", "", raw)
    if "," in text and "." in text:
        decimal = "." if text.rfind(".") > text.rfind(",") else ","
        grouping = "," if decimal == "." else "."
        return float(text.replace(grouping, "").replace(decimal, "."))
    separator = "," if "," in text else "."
    if separator not in text:
        return float(text)
    grouped = float(re.sub(r"[.,]", "", text)) if GROUPED_RE.fullmatch(text) else None
    single = text.count(separator) == 1
    plain = float(text.replace(",", ".")) if single else None
    if grouped is None:
        return plain
    if plain is None:
        return grouped
    if price is None:
        return grouped if separator == "," else plain
    return min((plain, grouped), key=lambda value: abs(math.log(value / price)) if value > 0 else math.inf)


def _checked(raws: list[str], price: float | None, warnings: list[str]) -> list[float]:
    values = []
    for raw in raws:
        value = resolve_number(raw, price)
        if value is None:
            continue
        if price and not 1 / MAX_PRICE_RATIO <= value / price <= MAX_PRICE_RATIO:
            if "LEVEL_FAR_FROM_PRICE" not in warnings:
                warnings.append("LEVEL_FAR_FROM_PRICE")
            continue
        values.append(value)
    return values


def apply_price(parsed: ParsedPost, price: float | None) -> ParsedPost:
    warnings = list(parsed.warnings)
    if price is None:
        warnings.append("PRICE_UNAVAILABLE")
    entries = _checked(parsed.raw_entry, price, warnings)
    stops = _checked(parsed.raw_stop, price, warnings)
    targets = _checked(parsed.raw_targets, price, warnings)
    stop = stops[0] if stops else None
    entry_low, entry_high = (min(entries), max(entries)) if entries else (None, None)
    side = parsed.side
    reference = (min(entries) + max(entries)) / 2 if entries else price
    if side is None and stop is not None and reference is not None and stop != reference:
        side = "LONG" if stop < reference else "SHORT"
        warnings.append("SIDE_FROM_LEVELS")
    return replace(parsed, side=side, entry_low=entry_low, entry_high=entry_high, stop=stop,
                   targets=targets, warnings=warnings)
