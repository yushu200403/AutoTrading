"""运行时组装回归测试。"""

from types import SimpleNamespace

from app import runtime


def test_runtime_assembles_single_service(monkeypatch):
    config = SimpleNamespace(
        BINANCE_API_KEY="binance-key",
        BINANCE_API_SECRET="binance-secret",
        AI_1_API_KEY="ai-key",
    )
    application = SimpleNamespace(extensions={})
    captured = {}

    class FakeEngine:
        def __init__(self, **kwargs):
            captured["engine_kwargs"] = kwargs

    class FakeService:
        def __init__(self, engine, app):
            self.engine = engine
            self.app = app

    monkeypatch.setattr(runtime, "get_config", lambda: config)
    monkeypatch.setattr(runtime, "create_app", lambda selected: application)
    monkeypatch.setattr(runtime, "TradingEngine", FakeEngine)
    monkeypatch.setattr(runtime, "TradingService", FakeService)
    monkeypatch.setattr(
        runtime,
        "init_service",
        lambda service: captured.setdefault("service", service),
    )

    result = runtime.create_runtime_app()

    assert result is application
    assert application.extensions["trading_service"] is captured["service"]
    assert captured["engine_kwargs"] == {
        "binance_api_key": "binance-key",
        "binance_api_secret": "binance-secret",
        "ai_api_key": "ai-key",
        "live_trading": None,
    }
