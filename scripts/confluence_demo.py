"""Synthetic confluence mechanics only; no market pattern detection or trade advice."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class Factor:
    kind: str
    low: float
    high: float
    direction: str
    timeframe: str
    detected_at: datetime
    rule_version: str
    dependency_group: str


def classify(factors: list[Factor], now: datetime, max_age: timedelta = timedelta(hours=8)) -> dict:
    if not factors:
        return {"status": "REJECTED", "reason": "no factors"}
    if any(f.low > f.high or f.direction not in {"LONG", "SHORT"} or
           not f.rule_version or f.detected_at > now or now - f.detected_at > max_age
           for f in factors):
        return {"status": "REJECTED", "reason": "invalid or stale factor"}
    if len({f.direction for f in factors}) != 1:
        return {"status": "REJECTED", "reason": "direction conflict"}
    low, high = max(f.low for f in factors), min(f.high for f in factors)
    if low > high:
        return {"status": "REJECTED", "reason": "no common price interval"}
    independent = len({f.dependency_group for f in factors})
    return {"status": "CANDIDATE" if independent >= 3 else "WATCHLIST",
            "zone": [low, high], "direction": factors[0].direction,
            "independent_groups": independent, "factor_count": len(factors),
            "note": "group count is not win probability"}


def demo() -> None:
    now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
    base = [Factor("liquidity", 100, 105, "LONG", "4H", now - timedelta(hours=2), "synthetic-v1", "swing-A"),
            Factor("fibonacci_extension", 102, 106, "LONG", "1H", now - timedelta(hours=1), "synthetic-v1", "swing-A"),
            Factor("fvg", 103, 104, "LONG", "15m", now - timedelta(minutes=30), "synthetic-v1", "gap-B")]
    assert classify(base, now)["status"] == "WATCHLIST"  # first two share a source swing
    assert classify(base, now)["zone"] == [103, 104]
    separate = [*base[:1], Factor(**{**base[1].__dict__, "dependency_group": "fib-C"}), base[2]]
    assert classify(separate, now)["status"] == "CANDIDATE"
    assert classify([*base[:2], Factor(**{**base[2].__dict__, "direction": "SHORT"})], now)["status"] == "REJECTED"
    assert classify([*base[:2], Factor(**{**base[2].__dict__, "detected_at": now - timedelta(hours=9)})], now)["status"] == "REJECTED"
    assert classify([*base[:2], Factor(**{**base[2].__dict__, "low": 107, "high": 108})], now)["status"] == "REJECTED"
    print("5 synthetic assertions passed; candidate:", classify(separate, now))


if __name__ == "__main__":
    demo()
