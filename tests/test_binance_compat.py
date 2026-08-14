"""币安原生算法订单接口兼容层测试。"""

from app.bot.binance_client import BinanceClient


class RawRequestExchange:
    def __init__(self):
        self.calls = []

    def request(self, path, api, method, params):
        self.calls.append((path, api, method, params))
        if method == "GET":
            return {"data": {"orders": [{"algoId": 7}]}}
        return {"code": 200, "msg": "success"}


def test_algo_api_falls_back_to_signed_raw_request():
    client = BinanceClient.__new__(BinanceClient)
    client.exchange = RawRequestExchange()

    assert client._get_open_algo_orders("BTC/USDT") == [{"algoId": 7}]
    client._delete_algo_order("BTC/USDT", "7")
    client._delete_all_open_algo_orders("BTC/USDT")

    assert client.exchange.calls == [
        ("openAlgoOrders", "fapiPrivate", "GET", {"symbol": "BTCUSDT"}),
        (
            "algoOrder",
            "fapiPrivate",
            "DELETE",
            {"symbol": "BTCUSDT", "algoId": "7"},
        ),
        (
            "algoOpenOrders",
            "fapiPrivate",
            "DELETE",
            {"symbol": "BTCUSDT"},
        ),
    ]
