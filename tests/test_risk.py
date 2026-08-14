"""配置化风控回归测试。"""

from types import SimpleNamespace

import pytest

from app.bot.risk import RiskEngine, RiskValidationError
from app.bot.xml_parser import ToolCall
from tests.conftest import TestConfig


class Broker:
    def fetch_positions(self):
        return []

    def fetch_balance(self):
        return {"free": 10000}


class LowBalanceConfig(TestConfig):
    RISK_MIN_FREE_BALANCE_USDT = 100


class LowBalanceBroker(Broker):
    def fetch_balance(self):
        return {"free": 0}


def _call(name, args):
    return ToolCall(name=name, info="测试", args=args, raw_json="{}")


def test_trade_requires_configured_protective_order():
    engine = RiskEngine(TestConfig)
    context = SimpleNamespace(assets={"BTC/USDT": object()})
    call = _call("trade_in", {
        "target": "BTC/USDT", "side": "LONG", "count_usdt": "100"
    })
    with pytest.raises(RiskValidationError, match="保护单"):
        engine.validate_batch([call], Broker(), context)


def test_trade_within_configured_limits_passes():
    engine = RiskEngine(TestConfig)
    context = SimpleNamespace(assets={"BTC/USDT": object()})
    calls = [
        _call("trade_in", {
            "target": "BTC/USDT",
            "side": "LONG",
            "count_usdt": "100",
            "stop_loss_price": "90",
        }),
        _call("update_memory", {"content": "等待下一周期"}),
    ]
    engine.validate_batch(calls, Broker(), context)


def test_missing_market_context_blocks_new_risk():
    engine = RiskEngine(TestConfig)
    call = _call("trade_in", {
        "target": "BTC/USDT",
        "side": "LONG",
        "count_usdt": "100",
        "stop_loss_price": "90",
    })
    with pytest.raises(RiskValidationError, match="核心行情"):
        engine.validate_batch([call], Broker(), SimpleNamespace(assets={}))


def test_low_balance_still_allows_close_and_cancel_actions():
    class PositionedBroker(LowBalanceBroker):
        def fetch_positions(self):
            return [{
                "symbol": "BTC/USDT",
                "side": "LONG",
                "notional": 100,
                "leverage": 1,
            }]

    calls = [
        _call("close_position", {
            "target": "BTC/USDT",
            "side": "LONG",
            "percentage": "100",
            "reason": "降低风险",
        }),
        _call("cancel_orders", {
            "target": "BTC/USDT",
            "order_type": "all",
        }),
    ]

    RiskEngine(LowBalanceConfig).validate_batch(calls, PositionedBroker())


class ProtectedPositionBroker(Broker):
    """持有单向多头仓位的经纪商替身。"""

    def fetch_positions(self):
        return [{
            "symbol": "BTC/USDT",
            "side": "LONG",
            "notional": 100,
            "leverage": 1,
        }]


def test_cancel_all_orders_on_open_position_is_rejected():
    calls = [_call("cancel_orders", {"target": "BTC/USDT", "order_type": "all"})]
    with pytest.raises(RiskValidationError, match="失去保护"):
        RiskEngine(TestConfig).validate_batch(calls, ProtectedPositionBroker())


def test_cancel_stop_loss_only_is_also_rejected():
    calls = [
        _call("cancel_orders", {"target": "BTC/USDT", "order_type": "stop_loss"})
    ]
    with pytest.raises(RiskValidationError, match="失去保护"):
        RiskEngine(TestConfig).validate_batch(calls, ProtectedPositionBroker())


def test_cancel_take_profit_only_is_allowed():
    """单独撤止盈属于让利润奔跑，止损仍在，不应拦截。"""
    calls = [
        _call("cancel_orders", {"target": "BTC/USDT", "order_type": "take_profit"})
    ]
    RiskEngine(TestConfig).validate_batch(calls, ProtectedPositionBroker())


def test_cancel_then_rebuild_protection_is_allowed():
    calls = [
        _call("cancel_orders", {"target": "BTC/USDT", "order_type": "all"}),
        _call("modify_position", {
            "target": "BTC/USDT",
            "side": "LONG",
            "stop_loss_price": "90",
        }),
    ]
    RiskEngine(TestConfig).validate_batch(calls, ProtectedPositionBroker())


def test_unknown_leverage_blocks_new_exposure():
    class UnknownLeverageBroker(Broker):
        def fetch_positions(self):
            return [{
                "symbol": "BTC/USDT",
                "side": "LONG",
                "notional": 100,
                "leverage": None,
            }]

    context = SimpleNamespace(assets={"BTC/USDT": object()})
    call = _call("trade_in", {
        "target": "BTC/USDT",
        "side": "LONG",
        "count_usdt": "100",
        "stop_loss_price": "90",
    })
    with pytest.raises(RiskValidationError, match="无法确认"):
        RiskEngine(TestConfig).validate_batch(
            [call], UnknownLeverageBroker(), context
        )


@pytest.mark.parametrize("name", ["close_position", "modify_position"])
def test_position_actions_reject_missing_position(name):
    args = {"target": "BTC/USDT", "side": "LONG"}
    if name == "close_position":
        args.update({"percentage": "100", "reason": "测试"})
    else:
        args["stop_loss_price"] = "90"

    with pytest.raises(RiskValidationError, match="不存在 BTC/USDT 的可用仓位"):
        RiskEngine(TestConfig).validate_batch([_call(name, args)], Broker())


def test_configured_notional_limits_are_enforced():
    context = SimpleNamespace(assets={"BTC/USDT": object()})
    over_single = _call("trade_in", {
        "target": "BTC/USDT",
        "side": "LONG",
        "count_usdt": "1001",
        "stop_loss_price": "90",
    })
    with pytest.raises(RiskValidationError, match="单笔金额"):
        RiskEngine(TestConfig).validate_batch([over_single], Broker(), context)

    calls = [
        _call("trade_in", {
            "target": "BTC/USDT",
            "side": "LONG",
            "count_usdt": "1000",
            "stop_loss_price": "90",
        }),
        _call("trade_in", {
            "target": "ETH/USDT",
            "side": "LONG",
            "count_usdt": "1000",
            "stop_loss_price": "45",
        }),
    ]

    class ExistingExposure(Broker):
        def fetch_positions(self):
            return [{
                "symbol": "BTC/USDT",
                "side": "SHORT",
                "notional": 3500,
                "leverage": 1,
            }]

    context = SimpleNamespace(
        assets={"BTC/USDT": object(), "ETH/USDT": object()}
    )
    with pytest.raises(RiskValidationError, match="总名义价值"):
        RiskEngine(TestConfig).validate_batch(calls, ExistingExposure(), context)
