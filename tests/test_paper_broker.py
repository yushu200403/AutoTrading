"""真实行情驱动模拟账本的集成回归测试。"""

from decimal import Decimal

import pytest

from app.bot.executor import TradeExecutor
from app.models import PaperExecution, PaperOrder
from app.bot.paper_broker import PaperBroker
from tests.conftest import TestConfig


def test_open_is_idempotent_and_take_profit_closes_position(app, market):
    with app.app_context():
        broker = PaperBroker(market, TestConfig)
        executor = TradeExecutor(broker)
        first = executor.open_position(
            "BTC/USDT",
            "LONG",
            1000,
            stop_loss_price=90,
            take_profit_price=110,
            client_order_id="cycle-1-open",
        )
        second = executor.open_position(
            "BTC/USDT",
            "LONG",
            1000,
            stop_loss_price=90,
            take_profit_price=110,
            client_order_id="cycle-1-open",
        )
        assert first.success and second.success
        assert first.order_id == second.order_id
        assert broker.fetch_positions()[0]["contracts"] == 10
        assert len(broker.get_open_orders("BTC/USDT")) == 2

        market.prices["BTC/USDT"] = Decimal("111")
        triggered = broker.process_pending_orders(["BTC/USDT"])
        assert len(triggered) == 1
        assert broker.fetch_positions() == []
        assert broker.fetch_balance()["total"] > 10000


def test_client_order_id_conflict_and_unrealized_loss_reduce_free_balance(
    app, market
):
    with app.app_context():
        broker = PaperBroker(market, TestConfig)
        broker.create_market_order(
            "BTC/USDT", "BUY", 1, "LONG", client_order_id="shared-client-id"
        )
        with pytest.raises(ValueError, match="不同的模拟订单"):
            broker.create_market_order(
                "BTC/USDT", "BUY", 2, "LONG", client_order_id="shared-client-id"
            )
        with pytest.raises(ValueError, match="超过持仓数量"):
            broker.create_market_order(
                "BTC/USDT", "SELL", 2, "LONG", client_order_id="over-close"
            )

        market.prices["BTC/USDT"] = Decimal("50")
        balance = broker.fetch_balance()
        assert balance["free"] == pytest.approx(9849.96)
        assert balance["total"] == pytest.approx(9949.96)


def test_pending_order_recovers_committed_execution_after_restart_window(app, market):
    with app.app_context():
        broker = PaperBroker(market, TestConfig)
        broker.create_market_order(
            "BTC/USDT", "BUY", 1, "LONG", client_order_id="recovery-open"
        )
        stop = broker.create_stop_loss_order(
            "BTC/USDT", "SELL", 1, 90, "LONG", "recovery-stop"
        )
        market.prices["BTC/USDT"] = Decimal("89")
        execution = broker.create_market_order(
            "BTC/USDT",
            "SELL",
            1,
            "LONG",
            client_order_id=f"trigger-{stop['id']}",
            source_order_id=stop["id"],
        )
        assert PaperOrder.query.filter_by(order_id=stop["id"]).one().status == "NEW"
        assert PaperExecution.query.filter_by(order_id=execution["id"]).count() == 1

        recovered = PaperBroker(market, TestConfig).process_pending_orders(["BTC/USDT"])

        assert [item["id"] for item in recovered] == [execution["id"]]
        assert PaperOrder.query.filter_by(order_id=stop["id"]).one().status == "FILLED"


def test_full_close_reports_partial_when_protection_cleanup_fails(
    app, market, monkeypatch
):
    with app.app_context():
        broker = PaperBroker(market, TestConfig)
        executor = TradeExecutor(broker)
        opened = executor.open_position(
            "BTC/USDT",
            "LONG",
            100,
            stop_loss_price=90,
            client_order_id="cleanup-open",
        )
        assert opened.success
        monkeypatch.setattr(
            broker,
            "cancel_position_orders",
            lambda *args: (_ for _ in ()).throw(RuntimeError("撤单接口不可用")),
        )

        result = executor.close_position(
            "BTC/USDT",
            100,
            position_side="LONG",
            client_order_id="cleanup-close",
        )

        assert result.success is False
        assert result.status == "PARTIAL"
        assert result.order_id is not None
        assert "平仓已成交" in result.error
        assert broker.fetch_positions() == []


def test_protection_failure_compensates_new_position(app, market, monkeypatch):
    with app.app_context():
        broker = PaperBroker(market, TestConfig)
        executor = TradeExecutor(broker)

        def fail_protection(*args, **kwargs):
            raise RuntimeError("保护单服务不可用")

        monkeypatch.setattr(broker, "create_stop_loss_order", fail_protection)
        result = executor.open_position(
            "BTC/USDT",
            "LONG",
            100,
            stop_loss_price=90,
            client_order_id="cycle-2-open",
        )
        assert not result.success
        assert result.status == "COMPENSATED"
        assert broker.fetch_positions() == []


def test_hedge_positions_require_explicit_side_and_persist(app, market):
    with app.app_context():
        broker = PaperBroker(market, TestConfig)
        broker.create_market_order(
            "BTC/USDT", "BUY", 1, "LONG", client_order_id="long-1"
        )
        broker.create_market_order(
            "BTC/USDT", "SELL", 2, "SHORT", client_order_id="short-1"
        )
        with pytest.raises(ValueError, match="明确指定"):
            broker.get_position_size("BTC/USDT")

        reloaded = PaperBroker(market, TestConfig)
        assert reloaded.get_position_size("BTC/USDT", "LONG")["contracts"] == 1
        assert reloaded.get_position_size("BTC/USDT", "SHORT")["contracts"] == 2


def test_partial_close_resizes_protection_and_stop_loss_triggers(app, market):
    with app.app_context():
        broker = PaperBroker(market, TestConfig)
        executor = TradeExecutor(broker)
        opened = executor.open_position(
            "BTC/USDT",
            "LONG",
            1000,
            stop_loss_price=90,
            take_profit_price=110,
            client_order_id="partial-open",
        )
        assert opened.success

        reduced = executor.close_position(
            "BTC/USDT",
            40,
            position_side="LONG",
            client_order_id="partial-close",
        )
        assert reduced.success
        assert broker.get_position_size("BTC/USDT", "LONG")["contracts"] == 6
        assert {order["amount"] for order in broker.get_open_orders("BTC/USDT")} == {6}

        market.prices["BTC/USDT"] = Decimal("89")
        triggered = broker.process_pending_orders(["BTC/USDT"])
        assert len(triggered) == 1
        assert broker.fetch_positions() == []
        statuses = {order.order_type: order.status for order in PaperOrder.query.all()}
        assert statuses == {
            "STOP_MARKET": "FILLED",
            "TAKE_PROFIT_MARKET": "CANCELLED",
        }


def test_leverage_margin_cancel_and_hedge_close(app, market):
    with app.app_context():
        broker = PaperBroker(market, TestConfig)
        executor = TradeExecutor(broker)
        assert executor.set_leverage("BTC/USDT", 10).success
        assert executor.set_margin_mode("BTC/USDT", "isolated").success

        long_result = executor.open_position(
            "BTC/USDT",
            "LONG",
            1000,
            stop_loss_price=90,
            client_order_id="hedge-long",
        )
        short_result = executor.open_position(
            "BTC/USDT",
            "SHORT",
            500,
            stop_loss_price=110,
            client_order_id="hedge-short",
        )
        assert long_result.success and short_result.success
        balance = broker.fetch_balance()
        assert balance["used"] == pytest.approx(150)
        assert balance["free"] == pytest.approx(9849.4)

        long_order = next(
            order for order in broker.get_open_orders("BTC/USDT")
            if order["positionSide"] == "LONG"
        )
        assert executor.cancel_order_by_id("BTC/USDT", long_order["id"]).success
        assert len(broker.get_open_orders("BTC/USDT")) == 1

        assert executor.close_position(
            "BTC/USDT", 100, position_side="LONG", client_order_id="close-long"
        ).success
        assert broker.get_position_size("BTC/USDT", "LONG") is None
        assert broker.get_position_size("BTC/USDT", "SHORT") is not None
        assert executor.close_position(
            "BTC/USDT", 100, position_side="SHORT", client_order_id="close-short"
        ).success
        assert broker.fetch_positions() == []


def test_modify_protection_compensates_when_old_order_cannot_cancel(
    app, market, monkeypatch
):
    with app.app_context():
        broker = PaperBroker(market, TestConfig)
        executor = TradeExecutor(broker)
        opened = executor.open_position(
            "BTC/USDT",
            "LONG",
            100,
            stop_loss_price=90,
            client_order_id="modify-open",
        )
        assert opened.success
        old_order_id = broker.get_open_orders("BTC/USDT")[0]["id"]
        original_cancel = broker.cancel_order_by_id

        def fail_old_order(symbol, order_id):
            if order_id == old_order_id:
                return {"success": False, "error": "旧单暂不可撤"}
            return original_cancel(symbol, order_id)

        monkeypatch.setattr(broker, "cancel_order_by_id", fail_old_order)
        result = executor.modify_position_tpsl(
            "BTC/USDT",
            stop_loss_price=92,
            position_side="LONG",
            client_order_id="modify-sl",
        )

        assert not result.success
        assert result.status == "COMPENSATED"
        remaining = broker.get_open_orders("BTC/USDT")
        assert [order["id"] for order in remaining] == [old_order_id]
