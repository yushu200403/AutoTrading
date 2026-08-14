"""
数据引擎 - 数据聚合的主协调器。

从币安收集数据并构建供 AI 决策的上下文。
"""

import logging
import math
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime

from config import get_config
from app.bot.tz_utils import utc_now
from app.bot.binance_client import (
    BinanceClient, 
    TickerData, 
    OrderBookData, 
    FundingRateData,
    LongShortRatioData
)
from app.bot.macro_data import MacroDataClient
from app.bot.indicators import (
    calculate_all_indicators, 
    format_indicator_summary,
    format_ohlcv_for_prompt,
    format_price,
    IndicatorSummary,
)
from app.bot.exceptions import DataFetchError, InsufficientDataError

logger = logging.getLogger(__name__)


def _finite_float(value, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是有效数字") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} 必须是有限数字")
    return result


@dataclass
class AssetContext:
    """单个资产的完整上下文。"""
    symbol: str
    ticker: TickerData
    order_book: OrderBookData
    funding_rate: FundingRateData
    indicators: IndicatorSummary
    # 多空持仓比率
    long_short_ratio: LongShortRatioData = None
    # 多时间周期 K 线数据 (用于 AI 上下文)
    ohlcv_1m: List[List] = None   # 1 分钟 K 线
    ohlcv_15m: List[List] = None  # 15 分钟 K 线
    ohlcv_1h: List[List] = None   # 1 小时 K 线
    ohlcv_4h: List[List] = None   # 4 小时 K 线
    ohlcv_1d: List[List] = None   # 1 日 K 线
    data_errors: List[str] = field(default_factory=list)


@dataclass
class MarketContext:
    """供 AI 决策的完整市场上下文。"""
    timestamp: datetime
    advance_decline_ratio: float
    assets: Dict[str, AssetContext]
    
    # Account data (optional, requires auth)
    account_balance: Optional[Dict[str, float]] = None
    positions: Optional[List[Dict]] = None
    
    # 挂单信息 (止损/止盈条件委托单)
    pending_orders: Optional[List[Dict]] = None
    
    # 记忆白板
    memory_content: str = ""
    trading_mode: str = "paper"
    data_errors: List[str] = field(default_factory=list)


class DataEngine:
    """
    主数据聚合引擎。
    
    协调从币安收集数据，并构建结构化上下文供 AI 消费。
    """
    
    def __init__(
        self,
        binance_api_key: str = '',
        binance_api_secret: str = ''
    ):
        """
        初始化数据引擎。
        
        Args:
            binance_api_key: 可选的 API Key (用于私有端点)
            binance_api_secret: 可选的 API Secret
        """
        self.config = get_config()
        
        # 初始化客户端
        self.binance = BinanceClient(binance_api_key, binance_api_secret)
        self.macro = MacroDataClient()
        
        # 跟踪的交易对；复制一份避免与全局配置共享同一列表对象
        self.symbols = list(self.config.TRADING_SYMBOLS)
    
    @staticmethod
    def resolve_account_equity(balance: Dict, positions: List[Dict]) -> tuple:
        """解读账户净值，返回（总净值，可用余额，未实现盈亏）。

        部分交易所返回的 total 不含未实现盈亏，
        此时按可用余额叠加浮动盈亏补齐，避免净值被低估。
        """
        total_equity = float(balance.get("total", 0) or 0)
        free_balance = float(balance.get("free", 0) or 0)
        unrealized = sum(
            float(position.get("unrealized_pnl") or 0)
            for position in (positions or [])
        )
        if total_equity == free_balance and unrealized != 0:
            total_equity = free_balance + unrealized
        return total_equity, free_balance, unrealized

    def fetch_asset_data(self, symbol: str) -> AssetContext:
        """
        获取单个资产的所有数据。
        
        Args:
            symbol: 交易对 (例如 'BTC/USDT')
            
        Returns:
            AssetContext 包含所有数据 (部分字段失败时可能使用默认值)
        """
        # 获取 ticker (必需 - 如果失败则无法继续)
        ticker = self.binance.fetch_ticker(symbol)
        
        data_errors = []

        # 获取订单簿（可选，但失败状态会显式传给 AI）
        try:
            order_book = self.binance.fetch_order_book(symbol, depth=10)
        except Exception as e:
            logger.debug("无法获取 %s 订单簿: %s", symbol, e)
            data_errors.append(f"订单簿不可用: {e}")
            order_book = OrderBookData(
                bids=[], asks=[],
                bid_ask_imbalance=0.0,
                spread=0.0,
                mid_price=ticker.last_price
            )
        
        # 获取资金费率 (可选 - 失败时使用默认值)
        try:
            funding_rate = self.binance.fetch_funding_rate(symbol)
        except Exception as e:
            logger.debug("无法获取 %s 资金费率: %s", symbol, e)
            data_errors.append(f"资金费率不可用: {e}")
            funding_rate = FundingRateData(
                symbol=symbol,
                funding_rate=0.0,
                funding_rate_annualized=0.0,
                next_funding_time=0
            )
        
        # 获取多空持仓比率 (可选 - 失败时使用默认值)
        try:
            long_short_ratio = self.binance.fetch_long_short_ratio(symbol)
        except Exception as e:
            logger.debug("无法获取 %s 多空比: %s", symbol, e)
            data_errors.append(f"多空比不可用: {e}")
            long_short_ratio = LongShortRatioData(
                symbol=symbol,
                long_account_ratio=0.5,
                short_account_ratio=0.5,
                long_short_ratio=1.0,
                top_trader_long_ratio=0.5,
                top_trader_short_ratio=0.5,
                timestamp=int(time.time() * 1000)
            )
        
        # 获取多时间周期 OHLCV
        display_floor = max(100, self.config.KLINE_DISPLAY_LIMIT)
        timeframes = {
            '1m': display_floor,
            '15m': display_floor,
            '1h': self.config.CANDLE_LIMIT,
            '4h': display_floor,
            '1d': display_floor,
        }
        
        ohlcv_data = {}
        for tf, limit in timeframes.items():
            try:
                request_limit = min(limit + 1, 1500)
                candles = self.binance.fetch_ohlcv(symbol, tf, limit=request_limit)
                ohlcv_data[tf] = candles[:-1] if len(candles) > 1 else []
            except Exception as e:
                logger.warning("无法获取 %s 的 %s K 线: %s", symbol, tf, e)
                ohlcv_data[tf] = []
                data_errors.append(f"{tf} K线不可用: {e}")
        
        # 使用 1h 数据计算指标 (保持现有指标计算逻辑)
        try:
            indicators = calculate_all_indicators(symbol, ohlcv_data.get('1h', []))
        except InsufficientDataError as e:
            raise DataFetchError("Binance 1h K线", symbol, str(e)) from e
        
        return AssetContext(
            symbol=symbol,
            ticker=ticker,
            order_book=order_book,
            funding_rate=funding_rate,
            indicators=indicators,
            long_short_ratio=long_short_ratio,
            ohlcv_1m=ohlcv_data.get('1m', []),
            ohlcv_15m=ohlcv_data.get('15m', []),
            ohlcv_1h=ohlcv_data.get('1h', []),
            ohlcv_4h=ohlcv_data.get('4h', []),
            ohlcv_1d=ohlcv_data.get('1d', []),
            data_errors=data_errors,
        )
    
    def fetch_macro_data(self) -> float:
        """
        获取宏观市场数据。
        
        Returns:
            advance_decline_ratio: 市场宽度指标
        """
        # 市场宽度
        try:
            breadth_data = self.binance.fetch_top_gainers_losers(50)
            advance_decline_ratio = breadth_data['advance_decline_ratio']
        except Exception as e:
            logger.warning("无法获取市场宽度: %s", e)
            advance_decline_ratio = None
        
        return advance_decline_ratio
    
    def fetch_account_data(self, account_provider=None) -> tuple:
        """
        获取账户余额和持仓 (需要认证)。
        
        Returns:
            返回（余额字典，持仓列表）
        """
        try:
            provider = account_provider or self.binance
            balance = provider.fetch_balance()
            positions = provider.fetch_positions()
            return balance, positions
        except Exception as e:
            raise DataFetchError("账户数据", reason=str(e)) from e
    
    def _fetch_pending_orders(self, account_provider=None) -> List[Dict]:
        """
        获取所有挂单（算法订单：止损/止盈）。
        
        使用币安私有 API 直接获取，确保数据最新。
        遵循透明法则：让 AI 能看到所有待执行的条件委托。
        
        Returns:
            挂单列表，每个订单包含 symbol, order_id, type, side, trigger_price
        """
        try:
            provider = account_provider or self.binance
            raw_orders = []
            if provider is self.binance:
                for symbol in self.symbols:
                    raw_orders.extend(
                        (symbol, order) for order in provider.get_open_orders(symbol)
                    )
            else:
                raw_orders.extend(
                    (order.get('symbol'), order)
                    for order in provider.get_open_orders()
                )

            pending = []
            for fallback_symbol, order in raw_orders:
                info = order.get('info') or {}
                order_id = order.get('id') or order.get('order_id')
                if not order_id:
                    raise ValueError("挂单缺少订单 ID")
                pending.append({
                    'symbol': order.get('symbol') or fallback_symbol,
                    'order_id': order_id,
                    'type': order.get('type') or order.get('order_type'),
                    'side': order.get('side'),
                    'position_side': (
                        order.get('positionSide')
                        or order.get('position_side')
                        or info.get('positionSide')
                    ),
                    'quantity': _finite_float(
                        order.get('amount', order.get('quantity', 0)) or 0,
                        "挂单数量",
                    ),
                    'trigger_price': _finite_float(
                        order.get('stopPrice', order.get('trigger_price', 0)) or 0,
                        "挂单触发价",
                    ),
                    'is_algo': bool(order.get('is_algo')),
                })
            return pending
        except Exception as e:
            raise DataFetchError("挂单数据", reason=str(e)) from e
    
    def aggregate(
        self,
        memory_content: str = "",
        account_provider=None,
        trading_mode: str = "paper",
    ) -> MarketContext:
        """
        将所有数据源聚合为完整的市场上下文。
        
        这是交易循环的主要入口点。
        所有数据在此方法中实时刷新，确保 AI 获得最新数据。
        
        Args:
            memory_content: 当前 AI 记忆白板内容
            
        Returns:
            MarketContext 包含所有聚合数据
        """
        start_time = time.time()
        
        # 首先同步时间
        logger.debug("开始数据聚合，同步时间...")
        self.binance.synchronize_time()
        
        # 获取宏观数据
        advance_decline_ratio = self.fetch_macro_data()
        data_errors = []
        if advance_decline_ratio is None:
            data_errors.append("市场宽度不可用")
        
        # 获取每个资产的数据
        assets = {}
        for symbol in self.symbols:
            try:
                asset_data = self.fetch_asset_data(symbol)
                assets[symbol] = asset_data
            except Exception as e:
                logger.warning("无法获取 %s 数据: %s", symbol, e)
                data_errors.append(f"{symbol} 核心行情不可用: {e}")

        if not assets:
            raise DataFetchError("核心行情", reason="所有配置交易对均不可用")
        
        # 获取账户数据
        balance, positions = self.fetch_account_data(account_provider)
        
        # 获取所有挂单（算法订单：止损/止盈）
        pending_orders = self._fetch_pending_orders(account_provider)
        
        elapsed = time.time() - start_time
        logger.info("数据聚合完成 (%.1fs, %d 个资产)", elapsed, len(assets))
        
        return MarketContext(
            timestamp=utc_now(),
            advance_decline_ratio=advance_decline_ratio,
            assets=assets,
            account_balance=balance,
            positions=positions,
            pending_orders=pending_orders,
            memory_content=memory_content,
            trading_mode=trading_mode,
            data_errors=data_errors,
        )
    
    def build_prompt_context(self, context: MarketContext) -> str:
        """
        构建供 AI 提示词使用的格式化上下文字符串。
        
        Args:
            context: 来自 aggregate() 的 MarketContext
            
        Returns:
            格式化的 AI 提示词字符串
        """
        sections = []
        
        # 宏观部分
        sections.append("=" * 10)
        sections.append("[市场上下文]")
        sections.append("=" * 10)
        sections.append(self.macro.format_macro_summary(
            context.advance_decline_ratio
        ))
        if context.data_errors:
            sections.append("数据质量告警:")
            sections.extend(f"- {error}" for error in context.data_errors)
        
        # 资产部分 (所有 5 个币种同等对待)
        sections.append("")
        sections.append("=" * 10)
        sections.append("[资产分析]")
        sections.append("=" * 10)
        
        # 使用配置的 K 线显示数量
        kline_limit = self.config.KLINE_DISPLAY_LIMIT
        
        for symbol, asset in context.assets.items():
            sections.append("")
            sections.append(format_indicator_summary(asset.indicators))
            if asset.data_errors:
                sections.append("  [数据质量] " + " | ".join(asset.data_errors))
            
            # 添加多时间周期 K 线数据 (含 RSI/MACD)
            if asset.ohlcv_1d:
                sections.append(format_ohlcv_for_prompt(asset.ohlcv_1d, '1d', limit=kline_limit))
            if asset.ohlcv_4h:
                sections.append(format_ohlcv_for_prompt(asset.ohlcv_4h, '4h', limit=kline_limit))
            if asset.ohlcv_1h:
                sections.append(format_ohlcv_for_prompt(asset.ohlcv_1h, '1h', limit=kline_limit))
            if asset.ohlcv_15m:
                sections.append(format_ohlcv_for_prompt(asset.ohlcv_15m, '15m', limit=kline_limit))
            if asset.ohlcv_1m:
                sections.append(format_ohlcv_for_prompt(asset.ohlcv_1m, '1m', limit=kline_limit))
            
            # 增强版市场深度信息
            ob = asset.order_book
            depth_info = (
                f"  [市场深度] 不平衡度: {ob.bid_ask_imbalance:+.2f} | "
                f"价差: ${ob.spread:.4f}"
            )
            depth_info += (
                f" | 买单量: {ob.cumulative_bid_volume:,.2f} | "
                f"卖单量: {ob.cumulative_ask_volume:,.2f}"
            )
            sections.append(depth_info)
            
            # 挂单墙信息 (如果检测到)
            if ob.bid_wall_price:
                sections.append(
                    f"    买单墙: ${ob.bid_wall_price:,.2f} ({ob.bid_wall_volume:,.2f})"
                )
            if ob.ask_wall_price:
                sections.append(
                    f"    卖单墙: ${ob.ask_wall_price:,.2f} ({ob.ask_wall_volume:,.2f})"
                )
            
            # 多空持仓比率
            if asset.long_short_ratio:
                ls = asset.long_short_ratio
                sentiment = "多头拥挤" if ls.long_short_ratio > 1.5 else ("空头拥挤" if ls.long_short_ratio < 0.67 else "均衡")
                sections.append(
                    f"  [情绪] 多空比: {ls.long_short_ratio:.2f} ({sentiment}) | "
                    f"账户: 多 {ls.long_account_ratio*100:.1f}% "
                    f"空 {ls.short_account_ratio*100:.1f}% | "
                    f"大户多头: {ls.top_trader_long_ratio*100:.1f}%"
                )
            
            # 资金费率
            sections.append(
                f"  [资金费率] 年化 {asset.funding_rate.funding_rate_annualized:+.2f}%"
            )
            
            # 手续费信息
            try:
                if context.trading_mode == "paper":
                    paper_fee = float(self.config.PAPER_TAKER_FEE_RATE)
                    fees = {"taker": paper_fee, "maker": paper_fee}
                else:
                    fees = self.binance.get_fees(symbol)
                taker_fee = fees.get('taker', 0.0) * 100
                maker_fee = fees.get('maker', 0.0) * 100
                sections.append(
                    f"  [手续费] 吃单: {taker_fee:.3f}% | 挂单: {maker_fee:.3f}%"
                )
            except Exception as e:
                logger.debug("无法获取 %s 手续费: %s", symbol, e)
        
        # 账户部分
        if context.account_balance:
            sections.append("")
            sections.append("=" * 10)
            sections.append("[账户]")
            sections.append("=" * 10)
            
            # 当前收益概览
            total_equity, free_balance, unrealized_pnl = self.resolve_account_equity(
                context.account_balance, context.positions
            )
            
            # 从历史快照中计算基准净值和 24 小时收益
            try:
                from app.models import EquitySnapshot
                first_snapshot = EquitySnapshot.get_first(context.trading_mode)
                base_equity = (
                    float(first_snapshot.total_equity)
                    if first_snapshot else total_equity
                )
                
                total_profit = total_equity - base_equity
                total_profit_pct = (total_profit / base_equity * 100) if base_equity > 0 else 0
                
                snapshot_24h = EquitySnapshot.get_24h_ago(context.trading_mode)
                if snapshot_24h:
                    equity_24h = float(snapshot_24h.total_equity)
                    profit_24h = total_equity - equity_24h
                    profit_24h_pct = (
                        profit_24h / equity_24h * 100 if equity_24h > 0 else 0
                    )
                else:
                    profit_24h = 0
                    profit_24h_pct = 0
            except Exception as e:
                logger.debug("无法计算收益快照: %s", e)
                base_equity = total_equity
                total_profit = 0
                total_profit_pct = 0
                profit_24h = 0
                profit_24h_pct = 0
            
            sections.append(
                f"余额: {total_equity:.2f} USDT (可用: {free_balance:.2f})"
            )
            sections.append(
                f"总收益: {total_profit:+.2f} USDT ({total_profit_pct:+.2f}%)"
            )
            sections.append(
                f"24 小时收益: {profit_24h:+.2f} USDT ({profit_24h_pct:+.2f}%)"
            )
            
            if context.positions:
                sections.append("当前持仓:")
                for pos in context.positions:
                    sections.append(
                        f"  - {pos['symbol']}: {pos['side']} {pos['contracts']} @ ${format_price(pos['entry_price'])}|"
                        f"未实现盈亏: ${pos['unrealized_pnl']:+.2f} "
                        f"({pos['percentage']:+.2f}%)"
                    )
            else:
                sections.append("当前无持仓。")
            
            # 挂单信息 (止损/止盈条件委托)
            if context.pending_orders:
                sections.append("")
                sections.append("当前挂单:")
                for order in context.pending_orders:
                    raw_type = str(order.get('type') or '').upper()
                    if "TAKE_PROFIT" in raw_type:
                        order_type = "止盈"
                    elif "STOP" in raw_type:
                        order_type = "止损"
                    else:
                        order_type = raw_type or "其他挂单"
                    sections.append(
                        f"  - {order['symbol']}: {order_type} {order['side']} "
                        f"@ ${format_price(order['trigger_price'])} (ID: {order['order_id']})"
                    )
        
        # 记忆白板
        if context.memory_content:
            sections.append("")
            sections.append("=" * 10)
            sections.append("[记忆白板]")
            sections.append("=" * 10)
            sections.append(context.memory_content)
        
        return "\n".join(sections)
    
    def to_dict(self, context: MarketContext) -> Dict[str, Any]:
        """
        将上下文转换为字典以存储到数据库。
        
        Args:
            context: MarketContext 对象
            
        Returns:
            可序列化的字典
        """
        return {
            'timestamp': context.timestamp.isoformat(),
            'advance_decline_ratio': context.advance_decline_ratio,
            'assets': {
                symbol: {
                    'price': asset.ticker.last_price,
                    'change_24h': asset.ticker.change_24h_percent,
                    'rsi': asset.indicators.rsi,
                    'trend': asset.indicators.trend.trend_direction
                }
                for symbol, asset in context.assets.items()
            }
        }
