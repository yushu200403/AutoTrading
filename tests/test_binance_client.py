"""Binance 客户端的离线协议与转换回归测试。"""

from types import SimpleNamespace
import time

import ccxt
import pytest

from app.bot import binance_client as binance_module
from app.bot.binance_client import BinanceClient
from app.bot.exceptions import AuthenticationError, OrderResultUnknownError


class FakeExchange:
    def __init__(self):
        self.load_calls = 0
        self.order_calls = []
        self.cancel_calls = []
        self.options = {}
        self._milliseconds = 1000
        self.markets = {
            "BTC/USDT": {
                "precision": {"price": 2, "amount": 3},
                "limits": {"cost": {"min": 5}},
            }
        }

    def fetch_time(self):
        return 1250

    def milliseconds(self):
        return self._milliseconds

    def load_markets(self):
        self.load_calls += 1
        return self.markets

    def market(self, symbol):
        return self.markets[symbol]

    def fetch_ohlcv(self, symbol, timeframe, limit):
        return [[1, 1, 2, 1, 2, 10]] * limit

    def fetch_ticker(self, symbol):
        return {
            "last": 100,
            "high": 110,
            "low": 90,
            "quoteVolume": 1000000,
            "percentage": 2,
            "timestamp": 1,
        }

    def fetch_tickers(self):
        return {
            "LOW/USDT:USDT": {"percentage": 10, "quoteVolume": 1},
            "BTC/USDT:USDT": {"percentage": 2, "quoteVolume": 1000},
            "ETH/USDT:USDT": {"percentage": -1, "quoteVolume": 900},
        "OTHER/BTC": {"percentage": 5, "quoteVolume": 9999},
        "NONE/USDT": {"percentage": None, "quoteVolume": 9999},
        "NAN/USDT": {"percentage": float("nan"), "quoteVolume": 9999},
        "INF/USDT": {"percentage": 1, "quoteVolume": float("inf")},
        }

    def fetch_order_book(self, symbol, limit):
        return {
            "bids": [[99, 100], [98, 1], [97, 1], [96, 1]],
            "asks": [[101, 4], [102, 4], [103, 4], [104, 4]],
        }

    def fetch_funding_rate(self, symbol):
        return {"fundingRate": 0.001, "fundingTimestamp": 123}

    def fapiDataGetGlobalLongShortAccountRatio(self, params):
        return [{
            "longAccount": "0.6",
            "shortAccount": "0.4",
            "longShortRatio": "1.5",
            "timestamp": "123",
        }]

    def fapiDataGetTopLongShortPositionRatio(self, params):
        return [{"longPosition": "0.7", "shortPosition": "0.3"}]

    def fetch_balance(self):
        return {"USDT": {"total": 1000, "free": 800, "used": 200}}

    def fetch_positions(self, symbols=None):
        return [
            {
                "symbol": "BTC/USDT:USDT",
                "side": "long",
                "contracts": 2,
                "notional": 200,
                "entryPrice": 90,
                "markPrice": 100,
                "unrealizedPnl": 20,
                "percentage": 10,
            },
            {"symbol": "ETH/USDT:USDT", "side": "short", "contracts": 0},
        ]

    def fapiPrivateV2GetAccount(self):
        return {"positions": [{"symbol": "BTCUSDT", "leverage": "10"}]}

    def amount_to_precision(self, symbol, value):
        return f"{value:.3f}"

    def price_to_precision(self, symbol, value):
        return f"{value:.2f}"

    def create_order(self, **kwargs):
        self.order_calls.append(kwargs)
        return {"id": f"order-{len(self.order_calls)}", "price": 100}

    def fapiPrivateGetOrder(self, params):
        return {"orderId": 7}

    def parse_order(self, raw, market):
        return {"id": str(raw["orderId"]), "symbol": "BTC/USDT"}

    def set_leverage(self, leverage, symbol):
        return {"symbol": symbol, "leverage": leverage}

    def set_margin_mode(self, mode, symbol):
        return {"symbol": symbol, "mode": mode}

    def fetch_open_orders(self, symbol=None):
        return [{"id": "normal-1", "type": "limit", "positionSide": "LONG"}]

    def cancel_order(self, order_id, symbol):
        self.cancel_calls.append((order_id, symbol))
        return {"id": order_id}

    def fapiPrivateDeleteAllOpenOrders(self, params):
        return [{"id": "normal-1"}]


def _client(exchange=None, auth=True):
    client = BinanceClient.__new__(BinanceClient)
    client.api_key = "key" if auth else ""
    client.api_secret = "secret" if auth else ""
    client.exchange = exchange or FakeExchange()
    client._markets_cache = None
    return client


def test_public_market_conversion_and_cache(monkeypatch):
    exchange = FakeExchange()
    client = _client(exchange)
    client.synchronize_time()
    # CCXT 从 options['timeDifference'] 读取偏移量，方向为本地时间减服务器时间
    assert exchange.options["timeDifference"] == -250
    assert client.load_markets() is client.load_markets()
    assert exchange.load_calls == 1
    assert len(client.fetch_ohlcv("BTC/USDT", "1h", 2)) == 2

    monkeypatch.setattr(
        binance_module,
        "get_config",
        lambda: SimpleNamespace(TIMEFRAMES=["1m", "1h"]),
    )
    assert set(client.fetch_ohlcv_multi_timeframe("BTC/USDT", limit=2)) == {
        "1m",
        "1h",
    }
    ticker = client.fetch_ticker("BTC/USDT")
    assert ticker.last_price == 100
    assert client.fetch_tickers(["BTC/USDT"])["BTC/USDT"].high_24h == 110

    order_book = client.fetch_order_book("BTC/USDT", depth=4)
    assert order_book.bid_ask_imbalance > 0
    assert order_book.spread == 2
    assert order_book.mid_price == 100
    assert order_book.bid_wall_price == 99
    assert client._detect_order_wall([[1, 1], [2, 1]]) == (None, 0.0)

    funding = client.fetch_funding_rate("BTC/USDT")
    assert funding.funding_rate_annualized == pytest.approx(109.5)
    ratio = client.fetch_long_short_ratio("BTC/USDT:USDT")
    assert ratio.long_short_ratio == 1.5
    assert ratio.top_trader_long_ratio == 0.7

    breadth = client.fetch_top_gainers_losers(2)
    assert breadth["advance_count"] == 1
    assert breadth["decline_count"] == 1
    assert breadth["advance_decline_ratio"] == 1
    assert breadth["gainers"][0][0] == "BTC/USDT"


def test_public_data_defaults_and_validation(monkeypatch):
    exchange = FakeExchange()
    client = _client(exchange)
    exchange.fetch_ticker = lambda symbol: {"last": 0}
    with pytest.raises(ValueError, match="行情价格无效"):
        client.fetch_ticker("BTC/USDT")
    exchange.fetch_ticker = lambda symbol: {"last": float("nan")}
    with pytest.raises(ValueError, match="有限数字"):
        client.fetch_ticker("BTC/USDT")
    exchange.fetch_ticker = lambda symbol: {"last": 100, "high": float("inf")}
    with pytest.raises(ValueError, match="有限数字"):
        client.fetch_ticker("BTC/USDT")

    exchange.fetch_order_book = lambda symbol, limit: {
        "bids": [[99, float("nan")]],
        "asks": [[101, 1]],
    }
    with pytest.raises(ValueError, match="买盘第 1 档数量 必须是有限数字"):
        client.fetch_order_book("BTC/USDT")

    exchange.fetch_order_book = lambda symbol, limit: {"bids": [], "asks": []}
    empty = client.fetch_order_book("BTC/USDT")
    assert empty.bid_ask_imbalance == 0
    assert empty.mid_price == 0

    exchange.fetch_funding_rate = lambda symbol: {
        "fundingRate": float("inf"),
        "fundingTimestamp": 123,
    }
    with pytest.raises(ValueError, match="资金费率 必须是有限数字"):
        client.fetch_funding_rate("BTC/USDT")

    exchange.fapiDataGetTopLongShortPositionRatio = lambda params: (_ for _ in ()).throw(
        RuntimeError("大户接口不可用")
    )
    assert client.fetch_long_short_ratio("BTC/USDT").top_trader_long_ratio == 0.5
    exchange.fapiDataGetGlobalLongShortAccountRatio = lambda params: (_ for _ in ()).throw(
        RuntimeError("全局接口不可用")
    )
    assert client.fetch_long_short_ratio("BTC/USDT").long_short_ratio == 1
    exchange.fapiDataGetGlobalLongShortAccountRatio = lambda params: [{
        "longAccount": "NaN",
        "shortAccount": "0.4",
        "longShortRatio": "1.5",
        "timestamp": "123",
    }]
    assert client.fetch_long_short_ratio("BTC/USDT").long_short_ratio == 1

    exchange.fetch_tickers = lambda: {
        "BTC/USDT": {"percentage": 1, "quoteVolume": 10}
    }
    assert client.fetch_top_gainers_losers(10)["advance_decline_ratio"] == 9999
    exchange.fetch_tickers = lambda: {}
    assert client.fetch_top_gainers_losers(10)["advance_decline_ratio"] == 1


def test_private_account_position_and_precision(monkeypatch):
    with pytest.raises(AuthenticationError):
        _client(auth=False).fetch_balance()

    client = _client()
    assert client.fetch_balance() == {"total": 1000, "free": 800, "used": 200}
    client.exchange.fetch_balance = lambda: {
        "USDT": {"total": float("inf"), "free": 800, "used": 200}
    }
    with pytest.raises(ValueError, match="账户总余额 必须是有限数字"):
        client.fetch_balance()
    client.exchange.fetch_balance = FakeExchange().fetch_balance
    positions = client.fetch_positions(["BTC/USDT"])
    assert len(positions) == 1
    assert positions[0]["side"] == "LONG"
    assert positions[0]["leverage"] == 10
    assert client._binance_to_ccxt_symbol("BTCUSDT") == "BTC/USDT"
    assert client._binance_to_ccxt_symbol("BTCUSD") == "BTCUSD"

    fallback = client._format_position(
        {"symbol": "BTC/USDT:USDT", "contracts": -2}, {}
    )
    assert fallback["side"] == "SHORT"
    assert fallback["contracts"] == 2

    assert client.get_precision("BTC/USDT") == {"price": 2, "amount": 3}
    assert client.amount_to_precision("BTC/USDT", 1.23456) == 1.235
    assert client.price_to_precision("BTC/USDT", 1.236) == 1.24
    assert client.get_fees("BTC/USDT") == {"taker": 0.0004, "maker": 0.0002}
    client.exchange.markets["BTC/USDT"]["taker"] = float("nan")
    with pytest.raises(ValueError, match="吃单手续费 必须是有限数字"):
        client.get_fees("BTC/USDT")
    client.exchange.markets["BTC/USDT"].pop("taker")
    client.exchange.amount_to_precision = lambda symbol, value: "Infinity"
    with pytest.raises(ValueError, match="数量精度结果 必须是有限数字"):
        client.amount_to_precision("BTC/USDT", 1)
    client.exchange.amount_to_precision = FakeExchange().amount_to_precision
    assert client.truncate_to_precision(1.239, 2) == 1.23
    with pytest.raises(ValueError, match="十进制位数"):
        client.truncate_to_precision(1.2, 0.1)
    assert client.get_min_notional("BTC/USDT") == 5
    assert client.calculate_quantity("BTC/USDT", 100, current_price=100) == 1
    with pytest.raises(ValueError, match="价格无效"):
        client.calculate_quantity("BTC/USDT", 100, current_price=0)

    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    assert client.get_position_size("BTC/USDT", "LONG")["contracts"] == 2


def test_order_creation_reconciliation_and_settings(monkeypatch):
    exchange = FakeExchange()
    client = _client(exchange)
    market = client.create_market_order(
        "BTC/USDT", "BUY", 1, "LONG", client_order_id="market-client"
    )
    assert market["id"] == "order-1"
    assert exchange.order_calls[0]["params"]["newClientOrderId"] == "market-client"

    stop = client.create_stop_loss_order(
        "BTC/USDT", "SELL", 1, 90.126, "LONG", "stop-client"
    )
    take_profit = client.create_take_profit_order(
        "BTC/USDT", "SELL", 1, 110.126, "LONG", "tp-client"
    )
    assert stop["id"] == "order-2"
    assert take_profit["id"] == "order-3"
    assert exchange.order_calls[1]["params"]["stopPrice"] == 90.13
    assert client.fetch_order_by_client_id("BTC/USDT", "known")["id"] == "7"
    assert client.fetch_order_by_client_id("BTC/USDT", "") is None

    exchange.create_order = lambda **kwargs: (_ for _ in ()).throw(
        ccxt.RequestTimeout("超时")
    )
    monkeypatch.setattr(
        client,
        "fetch_order_by_client_id",
        lambda symbol, order_id: {"id": "reconciled"},
    )
    assert client.create_market_order(
        "BTC/USDT", "BUY", 1, "LONG", "reconcile"
    )["id"] == "reconciled"
    monkeypatch.setattr(client, "fetch_order_by_client_id", lambda *args: None)
    with pytest.raises(OrderResultUnknownError):
        client.create_market_order("BTC/USDT", "BUY", 1, "LONG", "unknown")

    assert client.set_leverage("BTC/USDT", 5)["leverage"] == 5
    with pytest.raises(ValueError, match="1 到 125"):
        client.set_leverage("BTC/USDT", 126)
    assert client.set_margin_mode("BTC/USDT", "isolated")["mode"] == "isolated"
    exchange.set_margin_mode = lambda mode, symbol: (_ for _ in ()).throw(
        RuntimeError("No need to change margin type")
    )
    assert client.set_margin_mode("BTC/USDT", "cross") == {"info": "already_set"}
    with pytest.raises(ValueError, match="保证金模式"):
        client.set_margin_mode("BTC/USDT", "invalid")


def test_conditional_lookup_open_orders_and_cancellation(monkeypatch):
    exchange = FakeExchange()
    client = _client(exchange)
    monkeypatch.setattr(client, "fetch_order_by_client_id", lambda *args: None)
    monkeypatch.setattr(
        client,
        "_get_open_algo_orders",
        lambda symbol: [{
            "algoId": 2,
            "clientAlgoId": "conditional-client",
            "orderType": "STOP_MARKET",
            "side": "SELL",
            "positionSide": "LONG",
            "quantity": "1",
            "triggerPrice": "90",
            "algoStatus": "NEW",
        }],
    )
    found = client.fetch_conditional_by_client_id(
        "BTC/USDT", "conditional-client"
    )
    assert found["id"] == "2"
    assert found["is_algo"] is True

    open_orders = client.get_open_orders("BTC/USDT")
    assert {order["id"] for order in open_orders} == {"normal-1", "2"}

    monkeypatch.setattr(client, "_get_open_algo_orders", lambda symbol: [{}])
    with pytest.raises(RuntimeError, match="缺少 algoId"):
        client.get_open_orders("BTC/USDT")

    assert client.cancel_order_by_id("BTC/USDT", "normal-1")["type"] == "normal"
    exchange.cancel_order = lambda *args: (_ for _ in ()).throw(RuntimeError("非普通单"))
    monkeypatch.setattr(client, "_delete_algo_order", lambda *args: {"code": 200})
    assert client.cancel_order_by_id("BTC/USDT", "2")["type"] == "algo"

    orders = [
        {"id": "sl-long", "type": "STOP_MARKET", "positionSide": "LONG"},
        {"id": "tp-short", "type": "TAKE_PROFIT_MARKET", "positionSide": "SHORT"},
        {"id": "limit", "type": "limit", "positionSide": "LONG"},
    ]
    monkeypatch.setattr(client, "get_open_orders", lambda symbol: orders)
    monkeypatch.setattr(
        client,
        "cancel_order_by_id",
        lambda symbol, order_id: {"success": True, "order_id": order_id},
    )
    assert [order["id"] for order in client.cancel_position_orders(
        "BTC/USDT", "LONG"
    )] == ["sl-long"]

    monkeypatch.setattr(client, "get_open_orders", lambda symbol: [
        {"id": "normal-sl", "type": "STOP_MARKET", "info": {}},
        {
            "id": "algo-tp",
            "type": "TAKE_PROFIT_MARKET",
            "info": {"type": "TAKE_PROFIT_MARKET"},
            "is_algo": True,
        },
    ])
    exchange.cancel_order = FakeExchange().cancel_order
    assert [order["id"] for order in client.cancel_orders_by_type(
        "BTC/USDT", "stop_loss"
    )] == ["normal-sl"]
    assert [order["id"] for order in client.cancel_orders_by_type(
        "BTC/USDT", "take_profit"
    )] == ["algo-tp"]
    with pytest.raises(ValueError, match="不支持"):
        client.cancel_orders_by_type("BTC/USDT", "unknown")


def test_open_orders_without_symbol_include_each_configured_algo_market(monkeypatch):
    client = _client()
    client.trading_symbols = ["BTC/USDT", "ETH/USDT"]
    monkeypatch.setattr(
        client,
        "_get_open_algo_orders",
        lambda symbol: [{
            "algoId": "normal-1" if symbol == "BTC/USDT" else "algo-eth",
            "orderType": "STOP_MARKET",
            "side": "SELL",
            "positionSide": "LONG",
            "quantity": "1",
            "triggerPrice": "90",
            "algoStatus": "NEW",
        }],
    )

    orders = client.get_open_orders()

    assert len(orders) == 3
    assert [order["symbol"] for order in orders if order.get("is_algo")] == [
        "BTC/USDT",
        "ETH/USDT",
    ]
