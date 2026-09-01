"""应用配置。

所有交易边界均由环境变量控制；默认运行在模拟交易模式。
"""

import os
import re
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv


load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"环境变量 {name} 必须是布尔值")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"环境变量 {name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _env_decimal(
    name: str,
    default: str,
    minimum: str = "0",
    allow_zero: bool = True,
) -> Decimal:
    raw = os.getenv(name, default)
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"环境变量 {name} 必须是有效数字") from exc
    if not value.is_finite():
        raise ValueError(f"环境变量 {name} 必须是有限数字")
    lower = Decimal(minimum)
    if value < lower or (not allow_zero and value == 0):
        comparator = "大于" if not allow_zero and lower == 0 else "不小于"
        raise ValueError(f"环境变量 {name} 必须{comparator} {minimum}")
    return value


class Config:
    """基础配置。"""

    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret-key-change-in-prod")
    DEBUG = _env_bool("FLASK_DEBUG", False)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Strict"
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", False)

    DATABASE_URL = os.getenv("DATABASE_URL", "")
    SQLALCHEMY_DATABASE_URI = DATABASE_URL or "sqlite:///opennof1.db"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
    AUTO_CREATE_SCHEMA = _env_bool("AUTO_CREATE_SCHEMA", True)

    BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
    BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
    BINANCE_TESTNET = _env_bool("BINANCE_TESTNET", False)
    EXCHANGE_TIMEOUT_SECONDS = _env_int("EXCHANGE_TIMEOUT_SECONDS", 15, 1, 120)
    BINANCE_RECV_WINDOW_MS = _env_int("BINANCE_RECV_WINDOW_MS", 10000, 1000, 60000)

    AI_1_API_KEY = os.getenv("AI_1_API_KEY", "")
    AI_1_BASE_URL = os.getenv("AI_1_BASE_URL", "https://api.deepseek.com/v1")
    AI_1_MODEL = os.getenv("AI_1_MODEL", "deepseek-chat")
    AI_2_API_KEY = os.getenv("AI_2_API_KEY", "")
    AI_2_BASE_URL = os.getenv("AI_2_BASE_URL", "")
    AI_2_MODEL = os.getenv("AI_2_MODEL", "")
    AI_TIMEOUT_SECONDS = _env_int("AI_TIMEOUT_SECONDS", 60, 1, 300)
    AI_MAX_RETRIES = _env_int("AI_MAX_RETRIES", 2, 0, 5)
    AI_MAX_TOOL_CALLS = _env_int("AI_MAX_TOOL_CALLS", 10, 1, 50)
    AI_MAX_MEMORY_CHARS = _env_int("AI_MAX_MEMORY_CHARS", 4000, 100, 200000)
    AI_MAX_RESPONSE_TOKENS = _env_int("AI_MAX_RESPONSE_TOKENS", 8000, 256, 32000)
    AI_MAX_PROMPT_CHARS = _env_int("AI_MAX_PROMPT_CHARS", 120000, 10000, 2000000)
    AI_TEMPERATURE = float(_env_decimal("AI_TEMPERATURE", "0.2"))

    TRADING_MODE = os.getenv("TRADING_MODE", "paper").strip().lower()
    LIVE_TRADING_CONFIRMATION = os.getenv("LIVE_TRADING_CONFIRMATION", "")
    TRADING_SYMBOLS = [
        symbol.strip().upper()
        for symbol in os.getenv(
            "TRADING_SYMBOLS",
            "BTC/USDT,ETH/USDT,BNB/USDT,SOL/USDT,DOGE/USDT",
        ).split(",")
        if symbol.strip()
    ]
    TRADING_INTERVAL_MINUTES = _env_int("TRADING_INTERVAL_MINUTES", 3, 1, 1440)
    TIMEFRAMES = ["1m", "15m", "1h", "4h", "1d"]
    CANDLE_LIMIT = _env_int("CANDLE_LIMIT", 300, 200, 1500)
    KLINE_DISPLAY_LIMIT = _env_int("KLINE_DISPLAY_LIMIT", 30, 5, 300)

    PAPER_INITIAL_BALANCE_USDT = _env_decimal(
        "PAPER_INITIAL_BALANCE_USDT", "10000", allow_zero=False
    )
    PAPER_TAKER_FEE_RATE = _env_decimal("PAPER_TAKER_FEE_RATE", "0.0004")
    PAPER_DEFAULT_LEVERAGE = _env_int("PAPER_DEFAULT_LEVERAGE", 1, 1, 125)

    RISK_MAX_LEVERAGE = _env_int("RISK_MAX_LEVERAGE", 20, 1, 125)
    RISK_MAX_SINGLE_TRADE_USDT = _env_decimal(
        "RISK_MAX_SINGLE_TRADE_USDT", "1000", allow_zero=False
    )
    RISK_MAX_POSITION_NOTIONAL_USDT = _env_decimal(
        "RISK_MAX_POSITION_NOTIONAL_USDT", "3000", allow_zero=False
    )
    RISK_MAX_TOTAL_NOTIONAL_USDT = _env_decimal(
        "RISK_MAX_TOTAL_NOTIONAL_USDT", "5000", allow_zero=False
    )
    RISK_MIN_FREE_BALANCE_USDT = _env_decimal("RISK_MIN_FREE_BALANCE_USDT", "0")
    RISK_REQUIRE_PROTECTIVE_ORDER = _env_bool("RISK_REQUIRE_PROTECTIVE_ORDER", True)
    # 保护单触发价与现价的最小间距（百分比），避免开仓即被扫损
    RISK_MIN_PROTECTIVE_DISTANCE_PERCENT = _env_decimal(
        "RISK_MIN_PROTECTIVE_DISTANCE_PERCENT", "0.3"
    )
    CONSOLE_PASSWORD = os.getenv("CONSOLE_PASSWORD", "")
    CONSOLE_AUTH_ENABLED = _env_bool("CONSOLE_AUTH_ENABLED", True)
    # 默认公开只读业务数据；显式开启后，访客必须登录才能读取
    CONSOLE_READONLY_AUTH_ENABLED = _env_bool(
        "CONSOLE_READONLY_AUTH_ENABLED", False
    )
    CONSOLE_SESSION_TTL_MINUTES = _env_int(
        "CONSOLE_SESSION_TTL_MINUTES", 60, 1, 1440
    )
    CUSTOM_INSTRUCTIONS_MAX_CHARS = _env_int(
        "CUSTOM_INSTRUCTIONS_MAX_CHARS", 10000, 100, 100000
    )

    _tz_str = os.getenv("TIMEZONE", "+8")
    try:
        TIMEZONE_OFFSET = int(_tz_str.replace("+", ""))
        if not -12 <= TIMEZONE_OFFSET <= 14:
            raise ValueError
    except ValueError as exc:
        raise ValueError("环境变量 TIMEZONE 必须是 -12 到 +14 的整数偏移") from exc

    # 提示词与响应体积的粗略折算系数：1 token 约 1.5 个中文字符
    CHARS_PER_TOKEN = Decimal("1.5")
    # 模型响应除记忆白板外还需容纳分析、决策与其他工具调用
    MEMORY_TOKEN_BUDGET_RATIO = Decimal("0.7")
    # 单根 K 线在提示词中的平均字符数，用于体积静态估算
    KLINE_CHARS_PER_ROW = 65

    @classmethod
    def validate(cls) -> None:
        """在创建应用前验证相互依赖的配置。"""
        if cls.TRADING_MODE not in {"paper", "live"}:
            raise ValueError("TRADING_MODE 只能是 paper 或 live")
        if not cls.TRADING_SYMBOLS:
            raise ValueError("TRADING_SYMBOLS 至少需要一个交易对")
        if len(cls.TRADING_SYMBOLS) != len(set(cls.TRADING_SYMBOLS)):
            raise ValueError("TRADING_SYMBOLS 不能包含重复交易对")
        invalid_symbols = [
            symbol
            for symbol in cls.TRADING_SYMBOLS
            if re.fullmatch(r"[A-Z0-9]+/USDT", symbol) is None
        ]
        if invalid_symbols:
            raise ValueError(f"TRADING_SYMBOLS 包含无效 USDT 交易对: {invalid_symbols}")
        if cls.KLINE_DISPLAY_LIMIT > cls.CANDLE_LIMIT:
            raise ValueError("KLINE_DISPLAY_LIMIT 不能大于 CANDLE_LIMIT")
        if cls.RISK_MAX_POSITION_NOTIONAL_USDT > cls.RISK_MAX_TOTAL_NOTIONAL_USDT:
            raise ValueError("单仓名义价值上限不能大于总名义价值上限")
        if cls.RISK_MAX_SINGLE_TRADE_USDT > cls.RISK_MAX_POSITION_NOTIONAL_USDT:
            raise ValueError("单笔名义价值上限不能大于单仓名义价值上限")
        if not 0 <= cls.AI_TEMPERATURE <= 2:
            raise ValueError("AI_TEMPERATURE 必须在 0 到 2 之间")
        memory_char_budget = int(
            Decimal(cls.AI_MAX_RESPONSE_TOKENS)
            * cls.CHARS_PER_TOKEN
            * cls.MEMORY_TOKEN_BUDGET_RATIO
        )
        if cls.AI_MAX_MEMORY_CHARS > memory_char_budget:
            raise ValueError(
                f"AI_MAX_MEMORY_CHARS 不能超过 {memory_char_budget}，"
                f"否则记忆写入会被 AI_MAX_RESPONSE_TOKENS="
                f"{cls.AI_MAX_RESPONSE_TOKENS} 截断；"
                "请降低 AI_MAX_MEMORY_CHARS 或提高 AI_MAX_RESPONSE_TOKENS"
            )
        estimated_kline_chars = (
            len(cls.TRADING_SYMBOLS)
            * len(cls.TIMEFRAMES)
            * cls.KLINE_DISPLAY_LIMIT
            * cls.KLINE_CHARS_PER_ROW
        )
        if estimated_kline_chars > cls.AI_MAX_PROMPT_CHARS:
            raise ValueError(
                f"按当前配置，提示词中 K 线数据预计约 {estimated_kline_chars} 字符，"
                f"超过 AI_MAX_PROMPT_CHARS={cls.AI_MAX_PROMPT_CHARS}，"
                "会超出模型上下文窗口；请降低 KLINE_DISPLAY_LIMIT 或减少 TRADING_SYMBOLS"
            )
        if not 0 <= cls.RISK_MIN_PROTECTIVE_DISTANCE_PERCENT < 100:
            raise ValueError(
                "RISK_MIN_PROTECTIVE_DISTANCE_PERCENT 必须在 0（含）到 100（不含）之间"
            )
        if not 0 <= cls.PAPER_TAKER_FEE_RATE < 1:
            raise ValueError("PAPER_TAKER_FEE_RATE 必须在 0（含）到 1（不含）之间")
        if cls.PAPER_DEFAULT_LEVERAGE > cls.RISK_MAX_LEVERAGE:
            raise ValueError("模拟默认杠杆不能大于风控最大杠杆")
        backup_values = (
            cls.AI_2_API_KEY,
            cls.AI_2_BASE_URL,
            cls.AI_2_MODEL,
        )
        if any(backup_values) and not all(backup_values):
            raise ValueError("备用 AI 提供商的密钥、地址和模型必须同时配置")
        if cls.TRADING_MODE == "live":
            if cls.LIVE_TRADING_CONFIRMATION != "I_UNDERSTAND_REAL_ORDERS":
                raise ValueError(
                    "实盘模式必须提供确认值 LIVE_TRADING_CONFIRMATION"
                )
            if not cls.BINANCE_API_KEY or not cls.BINANCE_API_SECRET:
                raise ValueError("实盘模式必须配置币安 API 凭证")
        # 运维类校验放在交易配置之后，避免掩盖更根本的配置错误
        if cls.CONSOLE_AUTH_ENABLED and not cls.CONSOLE_PASSWORD:
            raise ValueError(
                "启用控制台认证时必须配置 CONSOLE_PASSWORD；"
                "如确认无需认证请显式设置 CONSOLE_AUTH_ENABLED=false"
            )
        if cls.CONSOLE_READONLY_AUTH_ENABLED and not cls.CONSOLE_AUTH_ENABLED:
            raise ValueError(
                "启用访客数据认证时必须同时启用 CONSOLE_AUTH_ENABLED"
            )


class DevelopmentConfig(Config):
    """开发环境配置。"""

    DEBUG = _env_bool("FLASK_DEBUG", True)


class ProductionConfig(Config):
    """生产环境配置。"""

    DEBUG = False
    AUTO_CREATE_SCHEMA = False
    # 生产环境默认只在 HTTPS 下投递会话 Cookie，可显式关闭以适配内网反代
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", True)

    @classmethod
    def validate(cls) -> None:
        super().validate()
        if not cls.AI_1_API_KEY:
            raise ValueError("生产环境必须配置 AI_1_API_KEY")
        if cls.SECRET_KEY == "dev-secret-key-change-in-prod":
            raise ValueError("生产环境必须配置 FLASK_SECRET_KEY")


config_map = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
}


def get_config():
    """根据环境获取配置类。"""
    env = os.getenv("FLASK_ENV", "development").strip().lower()
    if env not in config_map:
        raise ValueError(f"不支持的 FLASK_ENV: {env}")
    return config_map[env]
