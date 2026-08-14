"""环境变量配置解析回归测试。"""

import pytest

from config import Config, ProductionConfig, _env_bool, _env_decimal, _env_int


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_decimal_config_rejects_non_finite(monkeypatch, value):
    monkeypatch.setenv("TEST_DECIMAL", value)

    with pytest.raises(ValueError, match="有限数字"):
        _env_decimal("TEST_DECIMAL", "1")


def test_scalar_environment_parsers_reject_invalid_values(monkeypatch):
    monkeypatch.setenv("TEST_BOOL", "可能")
    with pytest.raises(ValueError, match="布尔值"):
        _env_bool("TEST_BOOL")
    monkeypatch.setenv("TEST_INT", "3.5")
    with pytest.raises(ValueError, match="整数"):
        _env_int("TEST_INT", 1, 1, 10)
    monkeypatch.setenv("TEST_INT", "11")
    with pytest.raises(ValueError, match="1 到 10"):
        _env_int("TEST_INT", 1, 1, 10)


def test_live_and_production_configuration_guards():
    class InvalidMode(Config):
        TRADING_MODE = "unknown"

    with pytest.raises(ValueError, match="paper 或 live"):
        InvalidMode.validate()

    class UnconfirmedLive(Config):
        TRADING_MODE = "live"
        LIVE_TRADING_CONFIRMATION = ""

    with pytest.raises(ValueError, match="确认"):
        UnconfirmedLive.validate()

    class LiveWithoutCredentials(Config):
        TRADING_MODE = "live"
        LIVE_TRADING_CONFIRMATION = "I_UNDERSTAND_REAL_ORDERS"
        BINANCE_API_KEY = ""
        BINANCE_API_SECRET = ""

    with pytest.raises(ValueError, match="币安 API 凭证"):
        LiveWithoutCredentials.validate()

    class MissingProductionAI(ProductionConfig):
        AI_1_API_KEY = ""
        SECRET_KEY = "secure"
        CONSOLE_PASSWORD = "secure"

    with pytest.raises(ValueError, match="AI_1_API_KEY"):
        MissingProductionAI.validate()

    class ValidProduction(ProductionConfig):
        AI_1_API_KEY = "ai-key"
        SECRET_KEY = "secure"
        CONSOLE_PASSWORD = "secure"

    ValidProduction.validate()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"AI_TEMPERATURE": 2.1}, "AI_TEMPERATURE"),
        ({"PAPER_TAKER_FEE_RATE": 1}, "PAPER_TAKER_FEE_RATE"),
        (
            {"PAPER_DEFAULT_LEVERAGE": 21, "RISK_MAX_LEVERAGE": 20},
            "默认杠杆",
        ),
        (
            {
                "RISK_MAX_POSITION_NOTIONAL_USDT": 6000,
                "RISK_MAX_TOTAL_NOTIONAL_USDT": 5000,
            },
            "单仓名义价值",
        ),
        (
            {
                "RISK_MAX_SINGLE_TRADE_USDT": 4000,
                "RISK_MAX_POSITION_NOTIONAL_USDT": 3000,
            },
            "单笔名义价值",
        ),
        ({"AI_2_API_KEY": "备用密钥"}, "必须同时配置"),
        ({"TRADING_SYMBOLS": ["BTC/USDT", "BTC/USDT"]}, "重复交易对"),
        ({"TRADING_SYMBOLS": ["BTC-USDT"]}, "无效 USDT 交易对"),
    ],
)
def test_cross_field_configuration_guards(overrides, message):
    values = {
        "TRADING_MODE": "paper",
        "TRADING_SYMBOLS": ["BTC/USDT"],
        "CANDLE_LIMIT": 300,
        "KLINE_DISPLAY_LIMIT": 100,
        "AI_TEMPERATURE": 0.2,
        "PAPER_TAKER_FEE_RATE": 0.0004,
        "PAPER_DEFAULT_LEVERAGE": 1,
        "RISK_MAX_LEVERAGE": 20,
        "RISK_MAX_POSITION_NOTIONAL_USDT": 3000,
        "RISK_MAX_TOTAL_NOTIONAL_USDT": 5000,
        "AI_2_API_KEY": "",
        "AI_2_BASE_URL": "",
        "AI_2_MODEL": "",
    }
    values.update(overrides)
    candidate = type("待校验配置", (Config,), values)

    with pytest.raises(ValueError, match=message):
        candidate.validate()
