"""订单编排与补偿语义回归测试。"""

import pytest

from app.bot.binance_client import TickerData
from app.bot.exceptions import OrderResultUnknownError
from app.bot.executor import TradeExecutor, _child_order_id
from tests.conftest import TestConfig


class FakeClient:
    """可按需注入失败的最小 Broker 协议实现。"""

    def __init__(self, price=100.0):
        self.price = price
        self.market_orders = []
        self.protective_orders = []
        self.cancelled = []
        self.open_orders = []
        self.position = None
        self.fail_stop_loss = None
        self.fail_take_profit = None
        self.fail_rollback = None
        self.fail_cancel = None

    def fetch_ticker(self, symbol):
        return TickerData(symbol, self.price, self.price, self.price, 1000.0, 0.0, 1)

    def get_min_notional(self, symbol):
        return 5.0

    def calculate_quantity(self, symbol, usdt_amount, current_price=None):
        return usdt_amount / self.price

    def amount_to_precision(self, symbol, value):
        return round(float(value), 6)

    def create_market_order(
        self, symbol, side, quantity, position_side, client_order_id=None
    ):
        if self.fail_rollback and client_order_id and "onf-" in client_order_id:
            raise self.fail_rollback
        order = {
            "id": f"m-{len(self.market_orders)}",
            "average": self.price,
            "side": side,
            "quantity": quantity,
            "positionSide": position_side,
            "clientOrderId": client_order_id,
        }
        self.market_orders.append(order)
        return order

    def create_stop_loss_order(
        self, symbol, side, quantity, stop_price, position_side, client_order_id=None
    ):
        if self.fail_stop_loss:
            raise self.fail_stop_loss
        order = {
            "id": f"sl-{len(self.protective_orders)}",
            "type": "STOP_MARKET",
            "stopPrice": stop_price,
            "positionSide": position_side,
        }
        self.protective_orders.append(order)
        return order

    def create_take_profit_order(
        self, symbol, side, quantity, take_profit_price, position_side,
        client_order_id=None,
    ):
        if self.fail_take_profit:
            raise self.fail_take_profit
        order = {
            "id": f"tp-{len(self.protective_orders)}",
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": take_profit_price,
            "positionSide": position_side,
        }
        self.protective_orders.append(order)
        return order

    def get_position_size(self, symbol, position_side=None):
        return self.position

    def get_open_orders(self, symbol=None):
        return list(self.open_orders)

    def cancel_order_by_id(self, symbol, order_id):
        if self.fail_cancel:
            raise self.fail_cancel
        self.cancelled.append(order_id)
        return {"success": True, "order_id": order_id, "type": "normal"}

    def cancel_orders_by_type(self, symbol, order_type):
        if self.fail_cancel:
            raise self.fail_cancel
        self.cancelled.append(order_type)
        return [{"id": "c-1"}]

    def cancel_position_orders(self, symbol, position_side):
        if self.fail_cancel:
            raise self.fail_cancel
        self.cancelled.append(position_side)
        return []


def _executor(client):
    return TradeExecutor(client, TestConfig)


def test_protective_prices_reject_wrong_side_and_too_close():
    client = FakeClient(price=100.0)
    executor = _executor(client)

    # 方向错误：多头止损不能高于现价
    wrong_side = executor.open_position(
        "BTC/USDT", "LONG", 100.0, stop_loss_price=101.0,
        client_order_id="cycle-1",
    )
    assert wrong_side.success is False
    assert "仓位方向不匹配" in wrong_side.error

    # 间距不足：配置要求至少 0.3%，即多头止损需不高于 99.7
    too_close = executor.open_position(
        "BTC/USDT", "LONG", 100.0, stop_loss_price=99.9,
        client_order_id="cycle-2",
    )
    assert too_close.success is False
    assert "间距不足" in too_close.error

    too_close_tp = executor.open_position(
        "BTC/USDT", "LONG", 100.0, take_profit_price=100.1,
        client_order_id="cycle-3",
    )
    assert too_close_tp.success is False
    assert "间距不足" in too_close_tp.error

    # 均未真正下单
    assert client.market_orders == []


def test_open_position_with_valid_protection_succeeds():
    client = FakeClient(price=100.0)
    result = _executor(client).open_position(
        "BTC/USDT", "LONG", 200.0,
        stop_loss_price=95.0,
        take_profit_price=110.0,
        client_order_id="cycle-ok",
    )
    assert result.success is True
    assert result.status == "SUCCESS"
    assert result.sl_order_id == "sl-0"
    assert result.tp_order_id == "tp-1"
    assert len(client.market_orders) == 1


def test_protection_failure_rolls_back_new_quantity():
    client = FakeClient(price=100.0)
    client.fail_stop_loss = RuntimeError("交易所拒绝止损单")

    result = _executor(client).open_position(
        "BTC/USDT", "LONG", 200.0, stop_loss_price=95.0,
        client_order_id="cycle-comp",
    )

    assert result.success is False
    assert result.status == "COMPENSATED"
    assert "已回补新增仓位" in result.error
    # 一笔开仓 + 一笔回补
    assert len(client.market_orders) == 2
    assert client.market_orders[1]["side"] == "SELL"


def test_protection_and_rollback_failure_is_critical():
    client = FakeClient(price=100.0)
    client.fail_stop_loss = RuntimeError("交易所拒绝止损单")
    client.fail_rollback = RuntimeError("回补下单失败")

    result = _executor(client).open_position(
        "BTC/USDT", "LONG", 200.0, stop_loss_price=95.0,
        client_order_id="cycle-critical",
    )

    assert result.success is False
    assert result.status == "CRITICAL"
    assert "仓位回补失败" in result.error


def test_unknown_order_result_is_not_retried():
    client = FakeClient(price=100.0)
    client.fail_rollback = None

    def raise_unknown(*args, **kwargs):
        raise OrderResultUnknownError("BTC/USDT", "BUY", "cid", "网络超时")

    client.create_market_order = raise_unknown
    result = _executor(client).open_position(
        "BTC/USDT", "LONG", 200.0, stop_loss_price=95.0,
        client_order_id="cycle-unknown",
    )
    assert result.success is False
    assert result.status == "UNKNOWN"


def test_cancel_operations_surface_unknown_results():
    client = FakeClient()
    client.fail_cancel = OrderResultUnknownError(
        "BTC/USDT", "CANCEL", "oid", "网络超时"
    )
    executor = _executor(client)

    batch = executor.cancel_orders("BTC/USDT", "all")
    assert batch.success is False
    assert batch.status == "UNKNOWN"

    single = executor.cancel_order_by_id("BTC/USDT", "oid")
    assert single.success is False
    assert single.status == "UNKNOWN"


def test_child_order_id_requires_parent():
    with pytest.raises(ValueError, match="父级客户端订单 ID"):
        _child_order_id(None, "rollback")
    # 相同输入必须稳定，幂等重放才能命中既有订单
    assert _child_order_id("cycle-1", "sl") == _child_order_id("cycle-1", "sl")
    assert _child_order_id("cycle-1", "sl") != _child_order_id("cycle-2", "sl")
    assert len(_child_order_id("cycle-1", "sl")) <= 36


def test_partial_close_resizes_protective_orders():
    client = FakeClient(price=100.0)
    client.position = {"side": "LONG", "contracts": 2.0}
    client.open_orders = [{
        "id": "sl-old",
        "type": "STOP_MARKET",
        "stopPrice": 95.0,
        "positionSide": "LONG",
    }]

    result = _executor(client).close_position(
        "BTC/USDT", 50, reason="减仓", position_side="LONG",
        client_order_id="cycle-partial",
    )

    assert result.success is True
    assert result.quantity == 1.0
    # 新保护单已建立，旧保护单被撤销
    assert any(order["id"].startswith("sl-") for order in client.protective_orders)
    assert "sl-old" in client.cancelled


def test_full_close_cancels_position_orders():
    client = FakeClient(price=100.0)
    client.position = {"side": "LONG", "contracts": 2.0}

    result = _executor(client).close_position(
        "BTC/USDT", 100, reason="全平", position_side="LONG",
        client_order_id="cycle-full",
    )

    assert result.success is True
    assert result.quantity == 2.0
    assert "LONG" in client.cancelled
