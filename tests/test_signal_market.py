import asyncio
import unittest
from datetime import datetime, timedelta, timezone

import httpx

from signal_bot.market import BybitClient, MarketError, closed_bars

T0 = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
MS = int(T0.timestamp() * 1000)
STEP = 300_000


def ok(result):
    return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result})


def client(handler):
    return BybitClient(httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://test"))


class PriceAtTest(unittest.TestCase):
    def test_close_of_the_minute_of_the_moment(self):
        minute = 60_000

        def handler(request):
            self.assertEqual(request.url.params["interval"], "1")
            return ok({"list": [[str(MS + minute), "9", "9", "9", "9", "1", "1"],
                                [str(MS), "1", "3", "1", "2", "1", "1"],
                                [str(MS - minute), "4", "4", "4", "4", "1", "1"]]})

        moment = T0 + timedelta(seconds=20)
        self.assertEqual(asyncio.run(client(handler).price_at("BTCUSDT", moment)), 2.0)

    def test_no_bars_is_a_market_error(self):
        with self.assertRaises(MarketError):
            asyncio.run(client(lambda request: ok({"list": []})).price_at("BTCUSDT", T0))


class ClosedBarsTest(unittest.TestCase):
    def rows(self):
        return [[str(MS + STEP), "4", "5", "3", "4", "1", "1"],
                [str(MS), "1", "3", "1", "2", "1", "1"],
                [str(MS - STEP), "2", "3", "1", "1", "1", "1"]]

    def test_bars_are_sorted_and_parsed(self):
        bars = closed_bars(self.rows(), T0 + timedelta(minutes=10))
        self.assertEqual([b.close for b in bars], [1, 2, 4])
        self.assertEqual(bars[0].start, T0 - timedelta(minutes=5))

    def test_unclosed_candle_is_excluded(self):
        bars = closed_bars(self.rows(), T0 + timedelta(minutes=7))
        self.assertEqual([b.close for b in bars], [1, 2])


class ClientTest(unittest.TestCase):
    def test_symbols_keep_only_trading_usdt_perpetuals(self):
        def handler(request):
            return ok({"list": [{"symbol": "BTCUSDT", "status": "Trading", "contractType": "LinearPerpetual"},
                                {"symbol": "ETHUSDT", "status": "Closed", "contractType": "LinearPerpetual"},
                                {"symbol": "BTCUSDT-26DEC26", "status": "Trading", "contractType": "LinearFutures"},
                                {"symbol": "BTCPERP", "status": "Trading", "contractType": "LinearPerpetual"}],
                       "nextPageCursor": ""})
        self.assertEqual(asyncio.run(client(handler).symbols()), {"BTCUSDT"})

    def test_symbols_follow_cursor(self):
        pages = []

        def handler(request):
            pages.append(request.url.params.get("cursor"))
            first = request.url.params.get("cursor") is None
            return ok({"list": [{"symbol": "BTCUSDT" if first else "ETHUSDT", "status": "Trading",
                                 "contractType": "LinearPerpetual"}], "nextPageCursor": "next" if first else ""})
        self.assertEqual(asyncio.run(client(handler).symbols()), {"BTCUSDT", "ETHUSDT"})
        self.assertEqual(pages, [None, "next"])

    def test_price(self):
        def handler(request):
            self.assertEqual(request.url.params["symbol"], "BTCUSDT")
            return ok({"list": [{"symbol": "BTCUSDT", "lastPrice": "77802.30"}]})
        self.assertEqual(asyncio.run(client(handler).price("BTCUSDT")), 77802.3)

    def test_bars_request_bounded_window(self):
        def handler(request):
            params = request.url.params
            self.assertEqual(params["interval"], "5")
            self.assertEqual(int(params["end"]) - int(params["start"]), 999 * STEP)
            return ok({"list": [[str(MS), "1", "3", "1", "2", "1", "1"]]})
        bars = asyncio.run(client(handler).bars("BTCUSDT", T0, T0 + timedelta(hours=1)))
        self.assertEqual(len(bars), 1)

    def test_api_error_raises_even_with_http_200(self):
        def handler(request):
            return httpx.Response(200, json={"retCode": 10006, "retMsg": "Too many visits", "result": {}})
        with self.assertRaises(MarketError):
            asyncio.run(client(handler).price("BTCUSDT"))

    def test_http_error_raises_market_error(self):
        with self.assertRaises(MarketError):
            asyncio.run(client(lambda request: httpx.Response(503)).price("BTCUSDT"))

    def test_network_error_raises_market_error(self):
        def handler(request):
            raise httpx.ConnectError("down")
        with self.assertRaises(MarketError):
            asyncio.run(client(handler).price("BTCUSDT"))


if __name__ == "__main__":
    unittest.main()
