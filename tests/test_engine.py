"""交易周期编排的集成回归测试。"""

from threading import Lock
from types import SimpleNamespace

import pytest

from app import db
from app.bot.ai_agent import AIAgentError, AIResponse
from app.bot.data_engine import MarketContext
from app.bot.engine import TradingEngine
from app.bot.executor import ExecutionResult
from app.bot.tz_utils import utc_now
from app.bot.xml_parser import ToolCall
from app.models import TradeDecision, TradingCycle
from tests.conftest import TestConfig


class FakeBroker:
    def process_pending_orders(self, symbols):
        return []

    def fetch_balance(self):
        return {"total": 10000, "free": 10000}

    def fetch_positions(self):
        return []


class FakeDataEngine:
    symbols = ["BTC/USDT"]

    def __init__(self, broker):
        self.binance = broker

    def aggregate(self, memory, account_provider, trading_mode):
        return MarketContext(
            timestamp=utc_now(),
            advance_decline_ratio=1.0,
            assets={},
            account_balance=account_provider.fetch_balance(),
            positions=[],
            pending_orders=[],
            memory_content=memory,
            trading_mode=trading_mode,
        )

    @staticmethod
    def build_prompt_context(context):
        return "测试行情上下文"

    @staticmethod
    def to_dict(context):
        return {"trading_mode": context.trading_mode, "assets": {}}


class FakeAgent:
    api_key = "test"

    def __init__(self, response):
        self.response = response
        self.calls = 0

    def analyze_with_messages(self, messages):
        self.calls += 1
        return self.response


class AllowAllRisk:
    @staticmethod
    def validate_batch(tool_calls, broker, market_context):
        return None


def _tool(name, **args):
    return ToolCall(name=name, info=f"执行 {name}", args=args, raw_json="{}")


def _engine(tool_calls):
    broker = FakeBroker()
    response = AIResponse(
        raw_response="测试理由<tooluse>{}</tooluse>",
        tool_calls=tool_calls,
        has_memory_update=True,
        model="test-model",
        provider="test-provider",
        usage={"total_tokens": 12},
    )
    engine = TradingEngine.__new__(TradingEngine)
    engine.config = TestConfig
    engine.data_engine = FakeDataEngine(broker)
    engine.ai_agent = FakeAgent(response)
    engine.paper_broker = broker
    engine._trading_mode = "paper"
    engine.executor = SimpleNamespace()
    engine.risk_engine = AllowAllRisk()
    engine._cycle_lock = Lock()
    return engine


def test_cycle_persists_intent_and_blocks_later_risk_actions(app, monkeypatch):
    tools = [
        _tool(
            "close_position",
            target="BTC/USDT",
            percentage=100,
            reason="测试失败",
            side="LONG",
        ),
        _tool(
            "trade_in",
            target="BTC/USDT",
            side="LONG",
            count_usdt=100,
            stop_loss_price=90,
            take_profit_price=110,
        ),
        _tool("update_memory", content="测试记忆"),
    ]
    engine = _engine(tools)
    executed = []
    observed_statuses = []

    def execute(tool_call, client_order_id):
        decision = TradeDecision.query.order_by(TradeDecision.id.desc()).first()
        observed_statuses.append(decision.execution_status)
        executed.append(tool_call.name)
        if tool_call.name == "close_position":
            return False, ExecutionResult(
                False,
                status="FAILED",
                symbol="BTC/USDT",
                side="LONG",
                error="测试执行失败",
            )
        if tool_call.name == "update_memory":
            return True, None
        raise AssertionError("失败后的增险工具不应被执行")

    monkeypatch.setattr(engine, "_execute_tool", execute)

    with app.app_context():
        result = engine.run_cycle()
        decisions = TradeDecision.query.order_by(TradeDecision.id).all()
        cycle = db.session.get(TradingCycle, result["cycle_id"])

        assert result["success"] is False
        assert result["actions"][1]["status"] == "SKIPPED"
        assert result["actions"][1]["executed"] is False
        assert executed == ["close_position", "update_memory"]
        assert observed_statuses == ["PENDING", "PENDING"]
        assert engine.ai_agent.calls == 1
        assert [item.execution_status for item in decisions] == [
            "FAILED",
            "SKIPPED",
            "SUCCESS",
        ]
        assert all(item.cycle_id == result["cycle_id"] for item in decisions)
        assert all(item.trading_mode == "paper" for item in decisions)
        assert cycle.status == "PARTIAL"


def test_cycle_failure_is_persisted_and_lock_is_released(app, monkeypatch):
    engine = _engine([])
    monkeypatch.setattr(
        engine.data_engine,
        "aggregate",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("行情聚合失败")),
    )

    with app.app_context():
        result = engine.run_cycle()
        cycle = db.session.get(TradingCycle, result["cycle_id"])
        assert result["success"] is False
        assert result["error"] == "行情聚合失败"
        assert cycle.status == "FAILED"
        assert cycle.error == "行情聚合失败"

    assert engine._cycle_lock.acquire(blocking=False)
    engine._cycle_lock.release()


def test_unresolved_intent_blocks_new_cycle(app):
    engine = _engine([])
    with app.app_context():
        db.session.add(
            TradeDecision(
                cycle_id="old-cycle",
                trading_mode="paper",
                symbol="BTC/USDT",
                action="LONG",
                execution_status="PENDING",
            )
        )
        db.session.commit()

        result = engine.run_cycle()

        assert result["success"] is False
        assert "必须人工核对交易执行端" in result["error"]
        assert result["halt_required"] is True
        assert engine.ai_agent.calls == 0


def test_execute_tool_dispatches_to_executor(app):
    engine = _engine([])
    calls = []

    def record(name, value):
        calls.append((name, value))
        return ExecutionResult(True, status="SUCCESS")

    engine.executor = SimpleNamespace(
        open_position=lambda **kwargs: record("open_position", kwargs),
        close_position=lambda **kwargs: record("close_position", kwargs),
        set_leverage=lambda symbol, leverage: record("set_leverage", leverage),
        set_margin_mode=lambda symbol, mode: record("set_margin_mode", mode),
        modify_position_tpsl=lambda *args, **kwargs: record("modify", kwargs),
        cancel_orders=lambda symbol, order_type: record("cancel_orders", order_type),
        cancel_order_by_id=lambda symbol, order_id: record("cancel_order", order_id),
    )

    with app.app_context():
        success, _ = engine._execute_tool(
            _tool(
                "trade_in",
                target="BTC/USDT",
                side="LONG",
                count_usdt="100",
                stop_loss_price="90",
            ),
            "cid-1",
        )
        assert success is True
        assert calls[0][0] == "open_position"
        assert calls[0][1]["amount_usdt"] == 100.0
        assert calls[0][1]["stop_loss_price"] == 90.0

        engine._execute_tool(
            _tool("set_leverage", target="BTC/USDT", leverage="5"), "cid-2"
        )
        assert calls[1] == ("set_leverage", 5)

        engine._execute_tool(
            _tool("cancel_orders", target="BTC/USDT", order_type="stop_loss"),
            "cid-3",
        )
        assert calls[2] == ("cancel_orders", "stop_loss")

        # 记忆更新走本地存储，不触达执行器
        success, execution = engine._execute_tool(
            _tool("update_memory", content="记录"), "cid-4"
        )
        assert success is True
        assert execution is None

        # 未知工具必须被明确拒绝
        success, execution = engine._execute_tool(
            _tool("unknown_tool", target="BTC/USDT"), "cid-5"
        )
        assert success is False
        assert execution.status == "FAILED"
        assert "未知工具" in execution.error


def test_ai_validation_retries_only_before_execution():
    tools = [_tool("update_memory", content="等待")]
    engine = _engine(tools)
    response = engine.ai_agent.response

    class RetryAgent:
        def __init__(self):
            self.calls = 0

        def analyze_with_messages(self, messages):
            self.calls += 1
            if self.calls == 1:
                raise AIAgentError("首次响应非法")
            return response

    engine.ai_agent = RetryAgent()
    result = {"tokens_used": 0, "market_context": object()}
    selected = engine._get_valid_ai_response("行情", "指令", result)
    assert selected is response
    assert engine.ai_agent.calls == 2
    assert result["validation_retries"] == 1
    assert result["tokens_used"] == 12

    engine.ai_agent = SimpleNamespace(
        analyze_with_messages=lambda messages: (_ for _ in ()).throw(
            AIAgentError("持续非法")
        )
    )
    with pytest.raises(RuntimeError, match="模型决策未通过校验"):
        engine._get_valid_ai_response("行情", "指令", {
            "tokens_used": 0,
            "market_context": object(),
        })


def test_mode_controls_status_and_close_all(app):
    engine = _engine([])
    paper_executor = object()
    live_executor = object()
    engine._executors = {"paper": paper_executor, "live": live_executor}
    engine.executor = paper_executor
    engine.config = SimpleNamespace(
        LIVE_TRADING_CONFIRMATION="",
        BINANCE_API_KEY="",
        BINANCE_API_SECRET="",
        CUSTOM_INSTRUCTIONS_MAX_CHARS=5,
    )
    with pytest.raises(RuntimeError, match="确认实盘"):
        engine.enable_live_trading(True)

    engine.config.LIVE_TRADING_CONFIRMATION = "I_UNDERSTAND_REAL_ORDERS"
    with pytest.raises(RuntimeError, match="缺少币安"):
        engine.enable_live_trading(True)
    engine.config.BINANCE_API_KEY = "key"
    engine.config.BINANCE_API_SECRET = "secret"
    engine.enable_live_trading(True)
    assert engine.live_trading
    assert engine.executor is live_executor
    engine.enable_live_trading(False)
    assert engine.executor is paper_executor

    with app.app_context():
        with pytest.raises(ValueError, match="长度上限"):
            engine.set_custom_instructions("123456")
        engine.set_custom_instructions("谨慎")
        status = engine.get_status()
        assert status["trading_mode"] == "paper"
        assert status["has_custom_instructions"] is True
        assert status["ai_connected"] is True

    positions = [
        {"symbol": "BTC/USDT", "side": "LONG"},
        {"symbol": "BTC/USDT", "side": "SHORT"},
    ]
    engine.paper_broker = SimpleNamespace(fetch_positions=lambda: positions)

    def close(symbol, percentage, reason, position_side, client_order_id):
        if position_side == "SHORT":
            return ExecutionResult(False, status="FAILED", error="平仓失败")
        return ExecutionResult(
            True,
            status="SUCCESS",
            order_id="close-long",
            quantity=1,
        )

    engine.executor = SimpleNamespace(
        close_position=close,
        cancel_orders=lambda symbol, order_type: ExecutionResult(
            True,
            status="SUCCESS",
            symbol=symbol,
            quantity=2,
        ),
    )
    results = engine.close_all_positions()
    assert len(results["closed"]) == 1
    assert results["cancelled"] == [{"symbol": "BTC/USDT", "count": 2}]
    assert results["errors"][0]["side"] == "SHORT"


def test_constructor_rejects_non_boolean_mode_before_initialization():
    with pytest.raises(TypeError, match="布尔值或空值"):
        TradingEngine(live_trading="true")
