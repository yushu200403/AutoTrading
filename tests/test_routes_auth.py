"""控制接口认证与 API 契约回归测试。"""

from datetime import timedelta

from app import db
from app.bot.binance_client import TickerData
from app.models import EquitySnapshot, TradeDecision, utc_now

from app.routes import init_service


class FakeEngine:
    live_trading = False
    trading_mode = "paper"


class FakeService:
    def __init__(self):
        self.engine = FakeEngine()
        self.live_trading = False
        self.starts = 0

    def start(self):
        self.starts += 1


class FakeBinance:
    def fetch_ticker(self, symbol):
        return TickerData(symbol, 100, 110, 90, 1000000, 2.5, 100)

    def fetch_ohlcv(self, symbol, timeframe, limit):
        return [[1, 90, 105, 85, 100, 1000]]

    def fetch_top_gainers_losers(self, limit):
        return {
            "advance_decline_ratio": 1.5,
            "gainers": [{"symbol": "BTC/USDT"}] * 4,
            "losers": [{"symbol": "ETH/USDT"}] * 4,
        }


class FakeBroker:
    def fetch_positions(self):
        return [{
            "symbol": "BTC/USDT",
            "side": "LONG",
            "unrealized_pnl": 100,
        }]

    def fetch_balance(self):
        return {"total": 1100, "free": 900}


class DetailedEngine:
    def __init__(self):
        self.live_trading = False
        self.trading_mode = "paper"
        self.data_engine = type("DataEngine", (), {})()
        self.data_engine.symbols = ["BTC/USDT"]
        self.data_engine.binance = FakeBinance()
        self.broker = FakeBroker()

    def close_all_positions(self):
        return {"closed": [{"symbol": "BTC/USDT"}], "cancelled": [], "errors": []}


class DetailedService:
    def __init__(self):
        self.engine = DetailedEngine()
        self.live_trading = False
        self.events = []

    def get_status(self):
        return {"running": False, "trading_mode": self.engine.trading_mode}

    def start(self):
        self.events.append("start")

    def stop(self, wait_seconds: float = 5):
        self.events.append("stop")
        return True

    def enable_live_trading(self, enable):
        self.events.append(("live", enable))
        self.live_trading = enable
        self.engine.live_trading = enable
        self.engine.trading_mode = "live" if enable else "paper"

    def set_custom_instructions(self, instructions):
        self.events.append(("instructions", instructions))

    def run_once(self):
        self.events.append("run_once")
        return {"success": True, "cycle_id": "test-cycle"}


def _login(client):
    response = client.post(
        "/api/verify-password", json={"password": "test-password"}
    )
    return {"X-CSRF-Token": response.get_json()["csrf_token"]}


def test_control_route_requires_session_and_csrf(app):
    service = FakeService()
    init_service(service)
    client = app.test_client()

    assert client.get("/api/auth-status").get_json() == {
        "authentication_required": True,
        "control_access": False,
    }
    assert client.post("/api/start").status_code == 401
    login = client.post(
        "/api/verify-password", json={"password": "test-password"}
    )
    assert login.status_code == 200
    csrf = login.get_json()["csrf_token"]
    assert client.get(
        "/api/auth-status", headers={"X-CSRF-Token": csrf}
    ).get_json()["control_access"] is True
    assert client.post("/api/start").status_code == 403

    response = client.post("/api/start", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert service.starts == 1


def test_invalid_password_and_security_headers(app):
    init_service(FakeService())
    client = app.test_client()

    login = client.post("/api/verify-password", json={"password": "错误密码"})
    assert login.status_code == 401
    assert client.post(
        "/api/verify-password", json={"password": "test-password"}
    ).status_code == 200
    csrf = client.post(
        "/api/start", headers={"X-CSRF-Token": "无效令牌"}
    )
    assert csrf.status_code == 403

    response = client.get("/", base_url="https://localhost")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Permissions-Policy"] == (
        "camera=(), microphone=(), geolocation=()"
    )
    assert "max-age=31536000" in response.headers["Strict-Transport-Security"]
    # 内容安全策略不得放开内联脚本，也不应依赖外部脚本源
    csp = response.headers["Content-Security-Policy"]
    assert "script-src 'self';" in csp
    assert "unsafe-inline" not in csp.split("style-src")[0]


def test_request_body_limit_is_enforced(app):
    """Flask 默认已存在 MAX_CONTENT_LENGTH 且为 None，必须显式覆盖才生效。"""
    assert app.config["MAX_CONTENT_LENGTH"] == 1024 * 1024
    client = app.test_client()
    oversized = client.post(
        "/api/verify-password",
        data=b"x" * (2 * 1024 * 1024),
        content_type="application/json",
    )
    assert oversized.status_code == 413


def test_read_and_control_api_contracts(app):
    service = DetailedService()
    init_service(service)
    with app.app_context():
        db.session.add_all([
            TradeDecision(
                symbol="BTC/USDT",
                action="LONG",
                display_info="测试开仓",
                tool_name="trade_in",
                tool_args='{"side":"LONG"}',
                ai_reasoning="测试理由",
                execution_status="SUCCESS",
                executed_price=100,
            ),
            TradeDecision(
                symbol="SYSTEM",
                action="MEMORY",
                display_info="测试无效参数记录",
                tool_name="update_memory",
                tool_args="非 JSON",
                ai_reasoning="等待",
                execution_status="SUCCESS",
            ),
            EquitySnapshot(
                timestamp=utc_now() - timedelta(hours=25),
                trading_mode="paper",
                total_equity=1000,
                free_balance=1000,
                unrealized_pnl=0,
                position_count=0,
            ),
            EquitySnapshot(
                timestamp=utc_now(),
                trading_mode="paper",
                total_equity=1100,
                free_balance=900,
                unrealized_pnl=100,
                position_count=1,
            ),
        ])
        db.session.commit()

    client = app.test_client()
    assert client.get("/").status_code == 200
    assert client.get("/settings").status_code == 302
    # 存活探针不需要认证，也不返回业务数据
    assert client.get("/healthz").get_json() == {"status": "ok"}
    # 余额、持仓、模型推理、指令与记忆等业务数据均允许访客只读访问
    for endpoint in (
        "/api/status",
        "/api/tickers",
        "/api/alpha",
        "/api/decisions",
        "/api/records",
        "/api/positions",
        "/api/memory",
        "/api/instructions",
        "/api/account-summary",
        "/api/equity-history",
    ):
        assert client.get(endpoint).status_code == 200, endpoint

    assert client.get("/api/status").get_json()["trading_mode"] == "paper"
    assert client.get("/api/tickers").get_json()[0]["sparkline"] == [100]
    assert len(client.get("/api/alpha").get_json()["top_gainers"]) == 3
    assert len(client.get("/api/decisions").get_json()) == 2
    assert len(client.get("/api/records?limit=99999").get_json()) == 2
    assert client.get("/api/positions").get_json()[0]["side"] == "LONG"
    assert client.get("/api/memory").status_code == 200
    assert client.get("/api/instructions").status_code == 200

    summary = client.get("/api/account-summary").get_json()
    assert summary["total_profit"] == 100
    assert summary["profit_24h"] == 100
    assert summary["trading_mode"] == "paper"
    history = client.get("/api/equity-history?limit=1").get_json()
    assert history["base_equity"] == 1000
    assert len(history["data"]) == 2

    # 页面禁用控件只是交互提示，服务端仍必须拒绝所有访客写操作
    guest_requests = (
        ("/api/start", {}),
        ("/api/stop", {}),
        ("/api/live", {"json": {"enable": True}}),
        ("/api/instructions", {"json": {"instructions": "高风险指令"}}),
        ("/api/run-once", {}),
        ("/api/close-all", {}),
        ("/api/logout", {}),
    )
    for endpoint, kwargs in guest_requests:
        assert client.post(endpoint, **kwargs).status_code == 401, endpoint
    assert service.events == []

    headers = _login(client)
    assert client.get("/api/auth-status", headers=headers).get_json() == {
        "authentication_required": True,
        "control_access": True,
    }
    assert client.post("/api/start", headers=headers).status_code == 200
    assert client.post("/api/stop", headers=headers).status_code == 200
    invalid_live = client.post(
        "/api/live", json={"enable": "false"}, headers=headers
    )
    assert invalid_live.status_code == 400
    assert client.post(
        "/api/live", json={"enable": True}, headers=headers
    ).get_json()["live_trading"] is True
    assert client.post(
        "/api/instructions", json={"instructions": []}, headers=headers
    ).status_code == 400
    assert client.post(
        "/api/instructions",
        json={"instructions": "保持低频"},
        headers=headers,
    ).status_code == 200
    assert client.post("/api/run-once", headers=headers).get_json()["success"] is True
    assert client.post("/api/close-all", headers=headers).get_json()["success"] is True
    assert client.post("/api/logout", headers=headers).status_code == 200
    assert client.get("/api/auth-status", headers=headers).get_json()[
        "control_access"
    ] is False
    assert client.post("/api/start", headers=headers).status_code == 401


def test_service_unavailable_and_expired_session(app):
    init_service(None)
    client = app.test_client()
    _login(client)
    assert client.get("/api/status").status_code == 503

    with client.session_transaction() as session:
        session["console_authenticated_at"] = 1
        session["csrf_token"] = "expired"
    assert client.get(
        "/api/auth-status", headers={"X-CSRF-Token": "expired"}
    ).get_json()["control_access"] is False
    assert client.post(
        "/api/start", headers={"X-CSRF-Token": "expired"}
    ).status_code == 401


def test_explicitly_disabled_control_auth_allows_operations(app):
    service = FakeService()
    init_service(service)
    app.config["CONSOLE_AUTH_ENABLED"] = False
    client = app.test_client()

    assert client.get("/api/auth-status").get_json() == {
        "authentication_required": False,
        "control_access": True,
    }
    assert client.post("/api/start").status_code == 200
    assert service.starts == 1


def test_control_conflicts_and_unknown_failures_return_json(app):
    service = DetailedService()
    init_service(service)
    client = app.test_client()
    headers = _login(client)

    service.run_once = lambda: (_ for _ in ()).throw(
        RuntimeError("后台交易循环运行中，不能执行单次周期")
    )
    response = client.post("/api/run-once", headers=headers)
    assert response.status_code == 400
    assert "后台交易循环运行中" in response.get_json()["error"]

    service.run_once = lambda: (_ for _ in ()).throw(ValueError("内部周期错误"))
    response = client.post("/api/run-once", headers=headers)
    assert response.status_code == 500
    assert response.get_json() == {"error": "单次交易周期失败"}

    service.start = lambda: (_ for _ in ()).throw(ValueError("内部启动错误"))
    response = client.post("/api/start", headers=headers)
    assert response.status_code == 500
    assert response.get_json() == {"error": "启动交易循环失败"}

    service.stop = lambda: (_ for _ in ()).throw(ValueError("内部停止错误"))
    response = client.post("/api/stop", headers=headers)
    assert response.status_code == 500
    assert response.get_json() == {"error": "停止交易循环失败"}

    service.enable_live_trading = lambda enable: (_ for _ in ()).throw(
        TypeError("内部模式错误")
    )
    response = client.post("/api/live", json={"enable": True}, headers=headers)
    assert response.status_code == 500
    assert response.get_json() == {"error": "切换交易模式失败"}

    service.set_custom_instructions = lambda value: (_ for _ in ()).throw(
        RuntimeError("内部保存错误")
    )
    response = client.post(
        "/api/instructions",
        json={"instructions": "测试"},
        headers=headers,
    )
    assert response.status_code == 500
    assert response.get_json() == {"error": "保存自定义指令失败"}
