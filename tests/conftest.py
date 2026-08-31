"""测试公共夹具。"""

from decimal import Decimal, ROUND_DOWN

import pytest

from app import create_app, db
from app.bot.binance_client import TickerData
from config import Config


class TestConfig(Config):
    TESTING = True
    SECRET_KEY = "test-secret"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {}
    AUTO_CREATE_SCHEMA = True
    CONSOLE_PASSWORD = "test-password"
    CONSOLE_AUTH_ENABLED = True
    SESSION_COOKIE_SECURE = False
    TRADING_MODE = "paper"
    LIVE_TRADING_CONFIRMATION = ""
    BINANCE_API_KEY = ""
    BINANCE_API_SECRET = ""
    AI_1_API_KEY = "test-ai-key"
    AI_1_BASE_URL = "https://ai.invalid/v1"
    AI_1_MODEL = "test-model"
    AI_2_API_KEY = ""
    AI_2_BASE_URL = ""
    AI_2_MODEL = ""
    AI_TIMEOUT_SECONDS = 3
    AI_MAX_RETRIES = 2
    AI_TEMPERATURE = 0.2
    AI_MAX_RESPONSE_TOKENS = 8000
    AI_MAX_PROMPT_CHARS = 120000
    TRADING_SYMBOLS = ["BTC/USDT", "ETH/USDT"]
    TRADING_INTERVAL_MINUTES = 3
    CANDLE_LIMIT = 300
    KLINE_DISPLAY_LIMIT = 30
    PAPER_INITIAL_BALANCE_USDT = Decimal("10000")
    PAPER_TAKER_FEE_RATE = Decimal("0.0004")
    PAPER_DEFAULT_LEVERAGE = 1
    RISK_MAX_LEVERAGE = 20
    RISK_MAX_SINGLE_TRADE_USDT = Decimal("1000")
    RISK_MAX_POSITION_NOTIONAL_USDT = Decimal("3000")
    RISK_MAX_TOTAL_NOTIONAL_USDT = Decimal("5000")
    RISK_MIN_FREE_BALANCE_USDT = Decimal("0")
    RISK_REQUIRE_PROTECTIVE_ORDER = True
    RISK_MIN_PROTECTIVE_DISTANCE_PERCENT = Decimal("0.3")
    AI_MAX_TOOL_CALLS = 10
    AI_MAX_MEMORY_CHARS = 4000
    CUSTOM_INSTRUCTIONS_MAX_CHARS = 10000


@pytest.fixture
def app():
    application = create_app(TestConfig)
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def market():
    return FakeMarket()


class FakeMarket:
    """仅提供模拟 Broker 所需的真实行情客户端协议。"""

    def __init__(self):
        self.prices = {"BTC/USDT": Decimal("100"), "ETH/USDT": Decimal("50")}

    def fetch_ticker(self, symbol):
        price = float(self.prices[symbol])
        return TickerData(symbol, price, price, price, 1000000, 0, 1)

    def amount_to_precision(self, symbol, value):
        return float(Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN))

    def price_to_precision(self, symbol, value):
        return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))

    def get_precision(self, symbol):
        return {"price": 2, "amount": 6}

    def truncate_to_precision(self, value, precision):
        unit = Decimal("1").scaleb(-precision)
        return float(Decimal(str(value)).quantize(unit, rounding=ROUND_DOWN))

    def get_min_notional(self, symbol):
        return 5.0

    def calculate_quantity(self, symbol, usdt_amount, current_price=None):
        price = Decimal(str(current_price)) if current_price else self.prices[symbol]
        return self.amount_to_precision(symbol, Decimal(str(usdt_amount)) / price)
