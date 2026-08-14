"""真实行情聚合与提示词构建回归测试。"""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app import db
from app.bot import data_engine as data_module
from app.bot.binance_client import (
    FundingRateData,
    LongShortRatioData,
    OrderBookData,
    TickerData,
)
from app.bot.data_engine import AssetContext, DataEngine
from app.bot.exceptions import DataFetchError
from app.bot.indicators import (
    BollingerBandsData,
    DivergenceData,
    IndicatorSummary,
    SupportResistanceData,
    TrendData,
)
from app.bot.paper_broker import PaperBroker
from app.bot.tz_utils import utc_now
from app.models import EquitySnapshot
from tests.conftest import TestConfig


class DataConfig:
    KLINE_DISPLAY_LIMIT = 100
    CANDLE_LIMIT = 200
    PAPER_TAKER_FEE_RATE = Decimal("0.0004")


def _candles(count):
    return [
        [index * 60000, 100, 102, 99, 101, 1000]
        for index in range(count)
    ]


def _neutral_indicators(symbol: str, price: float) -> IndicatorSummary:
    """构造中性指标摘要，仅用于隔离被测逻辑与指标计算。"""
    return IndicatorSummary(
        symbol=symbol,
        current_price=price,
        vwap=price,
        price_vs_vwap="NEUTRAL",
        trend=TrendData(
            ema_20=price,
            ema_50=price,
            ema_200=price,
            trend_direction="NEUTRAL",
            trend_strength="WEAK",
        ),
        bollinger=BollingerBandsData(
            upper=price * 1.02,
            middle=price,
            lower=price * 0.98,
            bandwidth=0.04,
            percent_b=0.5,
            is_squeeze=False,
        ),
        atr=price * 0.02,
        atr_percent=2.0,
        rsi=50.0,
        rsi_condition="NEUTRAL",
        divergence=DivergenceData(
            rsi_value=50.0,
            has_bullish_divergence=False,
            has_bearish_divergence=False,
            divergence_type="NONE",
        ),
        support_resistance=SupportResistanceData(
            supports=[price * 0.95],
            resistances=[price * 1.05],
            nearest_support=price * 0.95,
            nearest_resistance=price * 1.05,
        ),
    )


class FakeBinance:
    def __init__(self):
        self.ohlcv_limits = {}
        self.synced = 0
        self.fail_optional = False

    def synchronize_time(self):
        self.synced += 1

    def fetch_ticker(self, symbol):
        return TickerData(symbol, 100, 110, 90, 1000000, 1.5, 1)

    def fetch_order_book(self, symbol, depth):
        if self.fail_optional:
            raise RuntimeError("订单簿超时")
        return OrderBookData(
            bids=[[99, 10]],
            asks=[[101, 9]],
            bid_ask_imbalance=0.1,
            spread=2,
            mid_price=100,
            cumulative_bid_volume=10,
            cumulative_ask_volume=9,
            bid_wall_price=98,
            bid_wall_volume=20,
            ask_wall_price=102,
            ask_wall_volume=18,
        )

    def fetch_funding_rate(self, symbol):
        if self.fail_optional:
            raise RuntimeError("资金费率超时")
        return FundingRateData(symbol, 0.0001, 10.95, 1)

    def fetch_long_short_ratio(self, symbol):
        if self.fail_optional:
            raise RuntimeError("多空比超时")
        return LongShortRatioData(symbol, 0.6, 0.4, 1.6, 0.55, 0.45, 1)

    def fetch_ohlcv(self, symbol, timeframe, limit):
        self.ohlcv_limits[timeframe] = limit
        if self.fail_optional and timeframe == "1m":
            raise RuntimeError("1m K 线超时")
        return _candles(limit)

    def fetch_top_gainers_losers(self, limit):
        return {"advance_decline_ratio": 1.2}

    def fetch_balance(self):
        return {"total": 1000, "free": 900}

    def fetch_positions(self):
        return []

    def get_open_orders(self, symbol=None):
        return [{
            "id": "order-1",
            "type": "STOP_MARKET",
            "side": "sell",
            "positionSide": "LONG",
            "amount": 1,
            "stopPrice": 90,
            "is_algo": True,
        }]

    def get_fees(self, symbol):
        raise AssertionError("模拟模式不应读取实盘手续费")


class FakeMacro:
    @staticmethod
    def format_macro_summary(value):
        return f"市场宽度：{value}"


def _engine():
    engine = DataEngine.__new__(DataEngine)
    engine.config = DataConfig
    engine.binance = FakeBinance()
    engine.macro = FakeMacro()
    engine.symbols = ["BTC/USDT"]
    return engine


def test_asset_fetch_keeps_200_closed_candles_and_degrades_optional_data(
    monkeypatch
):
    engine = _engine()
    monkeypatch.setattr(
        data_module,
        "calculate_all_indicators",
        lambda symbol, candles: _neutral_indicators(symbol, 100),
    )

    asset = engine.fetch_asset_data("BTC/USDT")
    assert len(asset.ohlcv_1h) == 200
    assert engine.binance.ohlcv_limits["1h"] == 201
    assert asset.data_errors == []

    engine.binance.fail_optional = True
    degraded = engine.fetch_asset_data("BTC/USDT")
    assert degraded.order_book.bids == []
    assert degraded.funding_rate.funding_rate == 0
    assert degraded.long_short_ratio.long_short_ratio == 1
    assert degraded.ohlcv_1m == []
    assert len(degraded.data_errors) == 4


def test_aggregate_and_prompt_use_paper_account_semantics(app, monkeypatch):
    engine = _engine()
    indicator = _neutral_indicators("BTC/USDT", 100)
    asset = AssetContext(
        symbol="BTC/USDT",
        ticker=engine.binance.fetch_ticker("BTC/USDT"),
        order_book=engine.binance.fetch_order_book("BTC/USDT", 10),
        funding_rate=engine.binance.fetch_funding_rate("BTC/USDT"),
        indicators=indicator,
        long_short_ratio=engine.binance.fetch_long_short_ratio("BTC/USDT"),
        ohlcv_1m=[],
        ohlcv_15m=[],
        ohlcv_1h=[],
        ohlcv_4h=[],
        ohlcv_1d=[],
        data_errors=["测试可选数据告警"],
    )
    monkeypatch.setattr(engine, "fetch_asset_data", lambda symbol: asset)
    paper_provider = SimpleNamespace(
        fetch_balance=lambda: {"total": 1100, "free": 900},
        fetch_positions=lambda: [{
            "symbol": "BTC/USDT",
            "side": "LONG",
            "contracts": 1,
            "entry_price": 100,
            "unrealized_pnl": 100,
            "percentage": 100,
        }],
        get_open_orders=lambda: [{
            "symbol": "BTC/USDT",
            "order_id": "paper-sl",
            "type": "STOP_MARKET",
            "side": "sell",
            "position_side": "LONG",
            "quantity": 1,
            "trigger_price": 90,
            "is_algo": False,
        }],
    )

    with app.app_context():
        db.session.add(
            EquitySnapshot(
                timestamp=utc_now() - timedelta(hours=25),
                trading_mode="paper",
                total_equity=1000,
                free_balance=1000,
                unrealized_pnl=0,
                position_count=0,
            )
        )
        db.session.commit()
        context = engine.aggregate(
            "保持耐心", account_provider=paper_provider, trading_mode="paper"
        )
        prompt = engine.build_prompt_context(context)

    assert engine.binance.synced == 1
    assert context.advance_decline_ratio == 1.2
    assert context.pending_orders[0]["order_id"] == "paper-sl"
    assert "吃单: 0.040%" in prompt
    assert "总收益: +100.00 USDT" in prompt
    assert "测试可选数据告警" in prompt
    assert "保持耐心" in prompt
    assert engine.to_dict(context)["assets"]["BTC/USDT"]["trend"] == "NEUTRAL"


def test_aggregate_and_private_data_fail_closed(monkeypatch):
    engine = _engine()
    monkeypatch.setattr(engine, "fetch_asset_data", lambda symbol: (_ for _ in ()).throw(
        RuntimeError("行情不可用")
    ))
    with pytest.raises(DataFetchError, match="所有配置交易对均不可用"):
        engine.aggregate(account_provider=engine.binance)

    broken_provider = SimpleNamespace(
        fetch_balance=lambda: (_ for _ in ()).throw(RuntimeError("账户超时"))
    )
    with pytest.raises(DataFetchError, match="账户超时"):
        engine.fetch_account_data(broken_provider)

    broken_orders = SimpleNamespace(
        get_open_orders=lambda: (_ for _ in ()).throw(RuntimeError("挂单超时"))
    )
    with pytest.raises(DataFetchError, match="挂单超时"):
        engine._fetch_pending_orders(broken_orders)

    invalid_orders = SimpleNamespace(
        get_open_orders=lambda: [{
            "symbol": "BTC/USDT",
            "id": "invalid-order",
            "type": "STOP_MARKET",
            "amount": float("nan"),
            "stopPrice": 90,
        }]
    )
    with pytest.raises(DataFetchError, match="挂单数量 必须是有限数字"):
        engine._fetch_pending_orders(invalid_orders)


def test_actual_paper_orders_are_normalized_for_next_cycle(app, market):
    engine = _engine()
    with app.app_context():
        broker = PaperBroker(market, TestConfig)
        broker.create_market_order(
            "BTC/USDT", "BUY", 1, "LONG", client_order_id="prompt-open"
        )
        created = broker.create_stop_loss_order(
            "BTC/USDT", "SELL", 1, 90, "LONG", "prompt-stop"
        )

        pending = engine._fetch_pending_orders(broker)

    assert pending == [{
        "symbol": "BTC/USDT",
        "order_id": created["id"],
        "type": "STOP_MARKET",
        "side": "sell",
        "position_side": "LONG",
        "quantity": 1.0,
        "trigger_price": 90.0,
        "is_algo": False,
    }]
