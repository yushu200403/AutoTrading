"""组装 Web 应用与唯一交易服务实例。"""

from app import create_app
from app.bot.engine import TradingEngine
from app.bot.service import TradingService
from app.routes import init_service
from config import get_config


def create_runtime_app():
    """创建包含交易运行时的 Flask 应用。"""
    config = get_config()
    app = create_app(config)
    engine = TradingEngine(
        binance_api_key=config.BINANCE_API_KEY,
        binance_api_secret=config.BINANCE_API_SECRET,
        ai_api_key=config.AI_1_API_KEY,
        live_trading=None,
    )
    service = TradingService(engine, app)
    init_service(service)
    app.extensions["trading_service"] = service
    return app
