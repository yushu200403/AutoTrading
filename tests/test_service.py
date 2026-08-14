"""交易服务生命周期回归测试。"""

from types import SimpleNamespace

import pytest

from app.bot import service as service_module
from app.bot.service import TradingService


class FakeEngine:
    def __init__(self):
        self.config = SimpleNamespace(
            TRADING_INTERVAL_MINUTES=3,
            MAX_CONSECUTIVE_CYCLE_FAILURES=2,
        )
        self.live_trading = False
        self.sync_calls = 0
        self.run_results = [{"success": True}]
        self.enabled = []
        self.instructions = []
        self.data_engine = SimpleNamespace(
            binance=SimpleNamespace(synchronize_time=self._synchronize_time)
        )

    def _synchronize_time(self):
        self.sync_calls += 1

    def run_cycle(self):
        result = self.run_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def enable_live_trading(self, enable):
        self.enabled.append(enable)
        self.live_trading = enable

    def set_custom_instructions(self, instructions):
        self.instructions.append(instructions)

    def get_status(self):
        return {"trading_mode": "live" if self.live_trading else "paper"}


class FakeThread:
    def __init__(self, target, name, daemon):
        self.target = target
        self.name = name
        self.daemon = daemon
        self.alive = False
        self.join_calls = []

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def join(self, timeout):
        self.join_calls.append(timeout)
        self.alive = False


def test_service_start_stop_and_controls(app, monkeypatch):
    engine = FakeEngine()
    monkeypatch.setattr(service_module, "Thread", FakeThread)
    service = TradingService(engine, app)

    service.start()
    assert service.is_running
    assert engine.sync_calls == 1
    with pytest.raises(RuntimeError, match="已在运行"):
        service.start()
    with pytest.raises(RuntimeError, match="必须停止"):
        service.enable_live_trading(True)
    with pytest.raises(RuntimeError, match="后台交易循环运行中"):
        service.run_once()

    service.stop(wait_seconds=2)
    assert not service.is_running
    with pytest.raises(RuntimeError, match="未运行"):
        service.stop()

    service.enable_live_trading(True)
    service.set_custom_instructions("只做高流动性交易")
    assert engine.enabled == [True]
    assert engine.instructions == ["只做高流动性交易"]
    assert service.run_once() == {"success": True}
    status = service.get_status()
    assert status["running"] is False
    assert status["live_trading"] is True
    assert status["trading_mode"] == "live"


def test_trading_loop_records_failure_and_exception(app):
    engine = FakeEngine()
    service = TradingService(engine, app)
    engine.run_results = [{"success": False, "error": "模型不可用"}]

    original_run = engine.run_cycle

    def run_once_then_stop():
        result = original_run()
        service._stop_event.set()
        return result

    engine.run_cycle = run_once_then_stop
    service._trading_loop()
    assert service._last_error == "模型不可用"
    assert service._stop_event.is_set()

    failing_engine = FakeEngine()
    failing_engine.config.MAX_CONSECUTIVE_CYCLE_FAILURES = 1
    failing_engine.run_results = [RuntimeError("数据库不可用")]
    failing = TradingService(failing_engine, app)
    failing._trading_loop()
    assert failing._last_error == "数据库不可用"
    assert "连续 1 个周期未成功" in failing.halt_reason
    assert failing._stop_event.is_set()


def test_loop_halts_immediately_when_reconciliation_required(app):
    engine = FakeEngine()
    engine.run_results = [{
        "success": False,
        "halt_required": True,
        "error": "存在待对账交易意图 7",
    }]
    service = TradingService(engine, app)

    service._trading_loop()

    # 需人工核对时必须立即停摆，不得继续下一周期
    assert "待对账" in service.halt_reason
    assert service._last_error == service.halt_reason
    assert engine.run_results == []
    assert service._stop_event.is_set()


def test_loop_survives_transient_failure_and_recovers(app):
    """单个周期异常不得终止循环，否则机器人会静默停摆。"""
    engine = FakeEngine()
    engine.config.MAX_CONSECUTIVE_CYCLE_FAILURES = 5
    engine.run_results = [
        RuntimeError("行情接口抖动"),
        {"success": True},
    ]
    service = TradingService(engine, app)
    original_wait = service._wait_seconds
    service._wait_seconds = lambda interval, elapsed, failures: 0

    def stop_after_success():
        result = FakeEngine.run_cycle(engine)
        if not engine.run_results:
            service._stop_event.set()
        return result

    engine.run_cycle = stop_after_success
    service._trading_loop()

    # 第一轮异常被记录，第二轮成功后错误被清空
    assert service._last_error is None
    assert service.halt_reason is None
    assert engine.run_results == []
    service._wait_seconds = original_wait


def test_backoff_grows_with_consecutive_failures(app):
    service = TradingService(FakeEngine(), app)
    interval = 180

    # 成功时按剩余时间等待
    assert service._wait_seconds(interval, 20, 0) == 160
    assert service._wait_seconds(interval, 300, 0) == 0
    # 失败时指数退避并有上限
    assert service._wait_seconds(interval, 0, 1) == 360
    assert service._wait_seconds(interval, 0, 2) == 720
    assert service._wait_seconds(interval, 0, 99) == min(
        interval * 2 ** service.MAX_BACKOFF_MULTIPLIER, service.MAX_BACKOFF_SECONDS
    )
