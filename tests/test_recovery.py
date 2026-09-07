"""使用持久化模拟账本验证自动对账和修复的实际效果。"""

import json
from decimal import Decimal

import pytest

from app import db
from app.bot.exceptions import OrderResultUnknownError
from app.bot.executor import TradeExecutor
from app.bot.paper_broker import PaperBroker
from app.bot.recovery import DecisionRecovery
from app.models import PaperExecution, PaperOrder, TradeDecision
from tests.conftest import TestConfig
from tests.test_engine import _engine, _tool


def _setup(market):
    engine = _engine([_tool("update_memory", content="继续根据最新市场决策")])
    broker = PaperBroker(market, TestConfig)
    engine.paper_broker = broker
    engine.executor = TradeExecutor(broker, TestConfig)
    return engine, broker


def _intent(name="trade_in", client_id="original", status="UNKNOWN", **args):
    values = {"target": "BTC/USDT", "side": "LONG"}
    values.update(args)
    decision = TradeDecision(
        cycle_id="history", trading_mode="paper", symbol="BTC/USDT",
        action="LONG", tool_name=name, tool_args=json.dumps(values),
        client_order_id=client_id, execution_status=status,
    )
    db.session.add(decision)
    db.session.commit()
    return decision


def test_confirmed_open_repairs_protection_without_reopening(app, market):
    with app.app_context():
        engine, broker = _setup(market)
        broker.create_market_order("BTC/USDT", "BUY", 1, "LONG", "original")
        decision = _intent(stop_loss_price=90, take_profit_price=110)
        report = DecisionRecovery(engine).run()
        assert report == {"resolved": [decision.id], "pending": []}
        assert decision.execution_status == "RECONCILED"
        assert float(decision.executed_quantity) == 1
        assert PaperExecution.query.count() == 1
        assert len(broker.get_open_orders("BTC/USDT")) == 2
        assert json.loads(decision.recovery_state)["steps"]["sl"]["order_id"]
        assert DecisionRecovery(engine).run()["pending"] == []
        assert PaperOrder.query.count() == 2


def test_partial_close_resizes_remaining_protection(app, market):
    with app.app_context():
        engine, broker = _setup(market)
        broker.create_market_order("BTC/USDT", "BUY", 2, "LONG", "open")
        old = broker.create_stop_loss_order("BTC/USDT", "SELL", 2, 90, "LONG", "old-sl")
        broker.create_market_order("BTC/USDT", "SELL", 1, "LONG", "close")
        # 模拟实盘成交后保护单数量同步失败。
        PaperOrder.query.filter_by(order_id=old["id"]).one().quantity = 2
        db.session.commit()
        decision = _intent("close_position", "close", "PARTIAL", percentage=50)
        assert DecisionRecovery(engine).run()["resolved"] == [decision.id]
        orders = broker.get_open_orders("BTC/USDT")
        assert len(orders) == 1
        assert orders[0]["amount"] == 1
        assert orders[0]["id"] != old["id"]
        assert PaperExecution.query.count() == 2


def test_closed_position_cleans_only_its_own_direction(app, market):
    with app.app_context():
        engine, broker = _setup(market)
        broker.create_market_order("BTC/USDT", "BUY", 1, "LONG", "open")
        broker.create_market_order("BTC/USDT", "SELL", 1, "SHORT", "short")
        broker.create_stop_loss_order("BTC/USDT", "SELL", 1, 90, "LONG", "long-sl")
        broker.create_stop_loss_order("BTC/USDT", "BUY", 1, 110, "SHORT", "short-sl")
        broker.create_market_order("BTC/USDT", "SELL", 1, "LONG", "close")
        decision = _intent("close_position", "close", "PARTIAL", percentage=100)
        assert DecisionRecovery(engine).run()["resolved"] == [decision.id]
        assert [o["positionSide"] for o in broker.get_open_orders("BTC/USDT")] == ["SHORT"]


def test_repair_timeout_after_commit_is_resolved_without_duplicate(app, market, monkeypatch):
    with app.app_context():
        engine, broker = _setup(market)
        broker.create_market_order("BTC/USDT", "BUY", 1, "LONG", "original")
        decision = _intent(stop_loss_price=90)
        create = broker.create_stop_loss_order
        calls = []

        def timeout(*args, **kwargs):
            calls.append(kwargs["client_order_id"])
            create(*args, **kwargs)
            raise OrderResultUnknownError("BTC/USDT", "SELL", kwargs["client_order_id"], "响应丢失")

        monkeypatch.setattr(broker, "create_stop_loss_order", timeout)
        assert DecisionRecovery(engine).run()["pending"]
        decision_id = decision.id
        db.session.remove()
        # 新恢复实例从数据库读取进度，模拟重启后继续。
        assert DecisionRecovery(engine).run()["pending"] == []
        assert len(calls) == 1
        assert PaperOrder.query.count() == 1
        assert db.session.get(TradeDecision, decision_id).execution_status == "RECONCILED"


def test_unconfirmed_repair_is_not_repeated_and_model_keeps_running(app, market, monkeypatch):
    with app.app_context():
        engine, broker = _setup(market)
        broker.create_market_order("BTC/USDT", "BUY", 1, "LONG", "original")
        _intent(stop_loss_price=90)
        calls = []

        def timeout(*args, **kwargs):
            calls.append(kwargs["client_order_id"])
            raise OrderResultUnknownError("BTC/USDT", "SELL", kwargs["client_order_id"], "网络超时")

        monkeypatch.setattr(broker, "create_stop_loss_order", timeout)
        first = engine.run_cycle()
        second = engine.run_cycle()
        assert first["recovery"]["pending"] and second["recovery"]["pending"]
        assert first["memory_updated"] and second["memory_updated"]
        assert engine.ai_agent.calls == 2
        assert len(calls) == 1
        assert PaperExecution.query.count() == 1


def test_expired_trigger_is_reported_for_model_correction(app, market):
    with app.app_context():
        engine, broker = _setup(market)
        broker.create_market_order("BTC/USDT", "BUY", 1, "LONG", "original")
        _intent(stop_loss_price=90)
        market.prices["BTC/USDT"] = Decimal("80")
        issue = DecisionRecovery(engine).run()["pending"][0]
        assert "重新设价" in issue["error"]
        assert issue["blocked_tools"] == ["trade_in"]
        assert PaperOrder.query.count() == 0


def test_cancellation_recovery_does_not_cancel_new_orders(app, market):
    with app.app_context():
        engine, broker = _setup(market)
        broker.create_market_order("BTC/USDT", "BUY", 1, "LONG", "open")
        old = broker.create_stop_loss_order("BTC/USDT", "SELL", 1, 90, "LONG", "old")
        new = broker.create_stop_loss_order("BTC/USDT", "SELL", 1, 95, "LONG", "new")
        decision = _intent("cancel_orders", order_type="all")
        decision.recovery_state = json.dumps({"cancel_targets": [old["id"]]})
        db.session.commit()
        assert DecisionRecovery(engine).run()["resolved"] == [decision.id]
        assert [o["id"] for o in broker.get_open_orders("BTC/USDT")] == [new["id"]]


def test_unknown_order_only_isolates_related_symbol(app, market, monkeypatch):
    with app.app_context():
        engine, broker = _setup(market)
        _intent(stop_loss_price=90)
        engine.ai_agent.response.tool_calls = [
            _tool("trade_in", target="BTC/USDT", side="LONG", count_usdt=100),
            _tool("trade_in", target="ETH/USDT", side="LONG", count_usdt=100),
            _tool("update_memory", content="持续观察"),
        ]
        calls = []
        from app.bot.executor import ExecutionResult

        def execute(tool_call, client_id):
            calls.append(tool_call.args.get("target", "SYSTEM"))
            return True, ExecutionResult(True, status="SUCCESS")

        monkeypatch.setattr(engine, "_execute_tool", execute)
        result = engine.run_cycle()
        assert calls == ["ETH/USDT", "SYSTEM"]
        assert result["actions"][0]["status"] == "SKIPPED"
        assert engine.ai_agent.calls == 1


@pytest.mark.parametrize("status", ["PENDING", "UNKNOWN", "PARTIAL", "CRITICAL"])
def test_all_old_halt_statuses_allow_model_calls(app, market, status):
    with app.app_context():
        engine, _ = _setup(market)
        _intent(status=status)
        result = engine.run_cycle()
        assert result["memory_updated"]
        assert result["recovery"]["pending"]
        assert engine.ai_agent.calls == 1


def test_recovery_failure_does_not_prevent_other_records(app, market):
    with app.app_context():
        engine, _ = _setup(market)
        broken = _intent()
        broken.tool_args = "不是 JSON"
        memory = _intent("update_memory", "memory", "PENDING", content="旧记忆")
        db.session.commit()
        report = DecisionRecovery(engine).run()
        assert report["resolved"] == [memory.id]
        assert report["pending"][0]["decision_id"] == broken.id


def test_paper_queries_include_executed_and_cancelled_orders(app, market):
    with app.app_context():
        _, broker = _setup(market)
        broker.create_market_order("BTC/USDT", "BUY", 1, "LONG", "open")
        stop = broker.create_stop_loss_order("BTC/USDT", "SELL", 1, 90, "LONG", "stop")
        broker.cancel_order_by_id("BTC/USDT", stop["id"])
        assert broker.fetch_order_by_client_id("BTC/USDT", "open")["filled"] == 1
        assert broker.fetch_conditional_by_client_id("BTC/USDT", "stop")["status"] == "cancelled"
        assert broker.fetch_order_by_client_id("ETH/USDT", "open") is None
        assert broker.fetch_conditional_by_client_id("ETH/USDT", "stop") is None


def test_definitive_repair_rejection_can_retry(app, market, monkeypatch):
    with app.app_context():
        engine, broker = _setup(market)
        broker.create_market_order("BTC/USDT", "BUY", 1, "LONG", "original")
        decision = _intent(stop_loss_price=90)
        create = broker.create_stop_loss_order
        calls = []

        def reject_once(*args, **kwargs):
            calls.append(kwargs["client_order_id"])
            if len(calls) == 1:
                raise ValueError("保护单参数暂时被拒绝")
            return create(*args, **kwargs)

        monkeypatch.setattr(broker, "create_stop_loss_order", reject_once)
        issue = DecisionRecovery(engine).run()["pending"][0]
        assert issue["blocked_tools"] == ["trade_in"]
        assert DecisionRecovery(engine).run()["resolved"] == [decision.id]
        assert calls[0] == calls[1]
        assert PaperOrder.query.count() == 1


def test_query_outage_then_recovery_never_resends_market_order(app, market, monkeypatch):
    with app.app_context():
        engine, broker = _setup(market)
        broker.create_market_order("BTC/USDT", "BUY", 1, "LONG", "original")
        decision = _intent(stop_loss_price=90)
        lookup = broker.fetch_order_by_client_id
        monkeypatch.setattr(broker, "fetch_order_by_client_id", lambda *args: None)
        assert DecisionRecovery(engine).run()["pending"]
        assert PaperOrder.query.count() == 0
        monkeypatch.setattr(broker, "fetch_order_by_client_id", lookup)
        assert DecisionRecovery(engine).run()["resolved"] == [decision.id]
        assert PaperExecution.query.count() == 1


def test_uncertain_rollback_is_checked_before_protection_repair(app, market):
    with app.app_context():
        engine, broker = _setup(market)
        broker.create_market_order("BTC/USDT", "BUY", 1, "LONG", "original")
        decision = _intent(status="CRITICAL", stop_loss_price=90)
        decision.recovery_state = json.dumps({"uncertain": [{
            "client_order_id": "rollback", "conditional": False,
        }]})
        db.session.commit()
        assert DecisionRecovery(engine).run()["pending"]
        assert PaperOrder.query.count() == 0
        broker.create_market_order("BTC/USDT", "SELL", 1, "LONG", "rollback")
        assert DecisionRecovery(engine).run()["resolved"] == [decision.id]
        assert broker.fetch_positions() == []
        assert PaperExecution.query.count() == 2


def test_missing_side_is_recovered_from_confirmed_close(app, market):
    with app.app_context():
        engine, broker = _setup(market)
        broker.create_market_order("BTC/USDT", "BUY", 1, "LONG", "open")
        broker.create_market_order("BTC/USDT", "SELL", 1, "LONG", "close")
        decision = _intent("close_position", "close", "PARTIAL", side=None)
        assert DecisionRecovery(engine).run()["resolved"] == [decision.id]


def test_legacy_order_identity_is_reconstructed(app, market):
    with app.app_context():
        engine, broker = _setup(market)
        identity = engine._client_order_id("history", 1, "trade_in")
        _intent("update_memory", "memory", "SUCCESS")
        decision = _intent(client_id=None, stop_loss_price=90)
        broker.create_market_order("BTC/USDT", "BUY", 1, "LONG", identity)
        assert DecisionRecovery(engine).run()["resolved"] == [decision.id]


def test_zero_fill_is_not_reported_as_an_executed_trade(app, market, monkeypatch):
    with app.app_context():
        engine, broker = _setup(market)
        decision = _intent(stop_loss_price=90)
        monkeypatch.setattr(broker, "fetch_order_by_client_id", lambda *args: {
            "id": "rejected", "status": "rejected", "filled": 0,
        })
        assert DecisionRecovery(engine).run()["resolved"] == [decision.id]
        assert float(decision.executed_quantity) == 0
        assert PaperExecution.query.count() == 0
        assert PaperOrder.query.count() == 0


def test_take_profit_only_plan_keeps_original_risk_policy(app, market):
    with app.app_context():
        engine, broker = _setup(market)
        broker.create_market_order("BTC/USDT", "BUY", 1, "LONG", "original")
        decision = _intent(take_profit_price=110)
        assert DecisionRecovery(engine).run()["resolved"] == [decision.id]
        orders = broker.get_open_orders("BTC/USDT")
        assert len(orders) == 1
        assert orders[0]["type"] == "TAKE_PROFIT_MARKET"
