"""交易服务生命周期回归测试。"""

from types import SimpleNamespace

import pytest

from app.bot import service as service_module
from app.bot.service import TradingService


class FakeEngine:
    def __init__(self):
        self.config = SimpleNamespace(
            TRADING_INTERVAL_MINUTES=3,
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


def test_trading_loop_records_failed_result(app):
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
    """连续异常与失败结果不得终止循环，后续成功周期应正常执行。"""
    engine = FakeEngine()
    engine.run_results = [
        RuntimeError("行情接口抖动"),
        {"success": False, "error": "模型暂时不可用"},
        {"success": True},
    ]
    service = TradingService(engine, app)
    original_wait = service._wait_seconds
    service._wait_seconds = lambda interval, elapsed: 0

    def stop_after_success():
        result = FakeEngine.run_cycle(engine)
        if not engine.run_results:
            service._stop_event.set()
        return result

    engine.run_cycle = stop_after_success
    service._trading_loop()

    # 前两轮失败未停止循环，第三轮成功后错误被清空
    assert service._last_error is None
    assert service.halt_reason is None
    assert engine.run_results == []
    service._wait_seconds = original_wait


def test_wait_seconds_always_uses_configured_interval(app):
    service = TradingService(FakeEngine(), app)
    interval = 180

    assert service._wait_seconds(interval, 20) == 160
    assert service._wait_seconds(interval, 300) == 0
