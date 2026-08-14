"""
币安 USDT-M 合约客户端封装。

通过 CCXT 提供简洁的币安合约接口。
处理精度、错误处理和数据格式化。
"""

import logging
import math
import time
import ccxt
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from config import get_config
from app.bot.exceptions import AuthenticationError, OrderResultUnknownError

logger = logging.getLogger(__name__)


def _finite_float(value, field: str) -> float:
    """把交易所数值转换为有限浮点数。"""
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是有效数字") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} 必须是有限数字")
    return result


@dataclass
class OrderBookData:
    """包含不平衡计算的结构化订单簿数据。"""
    bids: List[List[float]]
    asks: List[List[float]]
    bid_ask_imbalance: float  # 正值 = 买单更多，负值 = 卖单更多
    spread: float
    mid_price: float
    # 增强字段：市场深度分析
    cumulative_bid_volume: float = 0.0  # 累积买单量
    cumulative_ask_volume: float = 0.0  # 累积卖单量
    bid_wall_price: Optional[float] = None  # 买单墙价格
    bid_wall_volume: float = 0.0  # 买单墙挂单量
    ask_wall_price: Optional[float] = None  # 卖单墙价格
    ask_wall_volume: float = 0.0  # 卖单墙挂单量


@dataclass
class TickerData:
    """结构化行情数据。"""
    symbol: str
    last_price: float
    high_24h: float
    low_24h: float
    volume_24h: float
    change_24h_percent: float
    timestamp: int


@dataclass
class FundingRateData:
    """资金费率数据。"""
    symbol: str
    funding_rate: float
    funding_rate_annualized: float
    next_funding_time: int


@dataclass
class LongShortRatioData:
    """多空持仓比率数据。"""
    symbol: str
    long_account_ratio: float  # 多头账户占比 (0-1)
    short_account_ratio: float  # 空头账户占比 (0-1)
    long_short_ratio: float  # 多空比 (>1 多头多, <1 空头多)
    top_trader_long_ratio: float  # 大户多头占比 (0-1)
    top_trader_short_ratio: float  # 大户空头占比 (0-1)
    timestamp: int


class BinanceClient:
    """
    币安 USDT-M 合约的 CCXT 封装。
    
    区分公共（无需认证）和私有（需认证）方法。
    """
    
    def __init__(self, api_key: str = '', api_secret: str = ''):
        """
        初始化币安客户端。
        
        Args:
            api_key: 币安 API Key（公共端点可选）
            api_secret: 币安 API Secret（公共端点可选）
        """
        config = get_config()
        
        # 优先使用显式参数，否则读取应用配置
        self.api_key = api_key or config.BINANCE_API_KEY
        self.api_secret = api_secret or config.BINANCE_API_SECRET
        self.trading_symbols = list(config.TRADING_SYMBOLS)
        
        # 初始化 CCXT 交易所客户端
        self.exchange = ccxt.binanceusdm({
            'apiKey': self.api_key,
            'secret': self.api_secret,
            'enableRateLimit': True,
            'timeout': config.EXCHANGE_TIMEOUT_SECONDS * 1000,
            # 双向持仓通过每笔请求显式传入 positionSide 实现，
            # CCXT 的 options 中没有对应的全局开关。
            'options': {
                'defaultType': 'future',
                'adjustForTimeDifference': True,
                'recvWindow': config.BINANCE_RECV_WINDOW_MS,
            }
        })
        if config.BINANCE_TESTNET:
            self.exchange.set_sandbox_mode(True)
            logger.warning("币安客户端正在使用测试网")
        
        # 缓存市场精度与限额信息
        self._markets_cache: Optional[Dict] = None
        
        # 首次同步服务器时间
        self.synchronize_time()
        
    def synchronize_time(self):
        """
        显式同步币安服务器时间，交由 CCXT 使用准确偏移量签名。

        CCXT 从 options['timeDifference'] 读取偏移量，
        取值方向与其 load_time_difference 保持一致（本地时间减服务器时间）。
        """
        try:
            server_time = self.exchange.fetch_time()
            local_time = self.exchange.milliseconds()
            difference = local_time - server_time
            self.exchange.options['timeDifference'] = difference
            logger.debug("时间同步完毕，本地时钟偏移: %d ms", difference)
        except Exception as e:
            logger.warning("时间同步失败: %s", e)
    
    # =========================================================================
    # 公共端点 (无需认证)
    # =========================================================================
    
    def load_markets(self) -> Dict:
        """加载并缓存市场信息。"""
        if self._markets_cache is None:
            self._markets_cache = self.exchange.load_markets()
        return self._markets_cache
    
    def fetch_ohlcv(
        self, 
        symbol: str, 
        timeframe: str = '1h', 
        limit: int = 300
    ) -> List[List]:
        """
        获取 OHLCV K线数据。
        
        Args:
            symbol: 交易对 (例如 'BTC/USDT')
            timeframe: K线间隔 ('1m', '5m', '15m', '1h', '4h', '1d')
            limit: K线数量 (最大 1500)
            
        Returns:
            由 [时间戳, 开盘价, 最高价, 最低价, 收盘价, 成交量] 组成的列表
        """
        return self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    
    def fetch_ohlcv_multi_timeframe(
        self, 
        symbol: str, 
        timeframes: List[str] = None,
        limit: int = 300
    ) -> Dict[str, List[List]]:
        """
        获取多个时间周期的 OHLCV 数据。
        
        Args:
            symbol: 交易对
            timeframes: 时间周期列表 (默认: 配置中的时间周期)
            limit: 每个时间周期的K线数量
            
        Returns:
            Dict 映射 timeframe -> OHLCV 数据
        """
        if timeframes is None:
            config = get_config()
            timeframes = config.TIMEFRAMES
        
        result = {}
        for tf in timeframes:
            result[tf] = self.fetch_ohlcv(symbol, tf, limit)
        
        return result
    
    def fetch_ticker(self, symbol: str) -> TickerData:
        """
        获取当前行情数据。
        
        Args:
            symbol: 交易对
            
        Returns:
            TickerData 包含当前价格和 24h 统计
        """
        ticker = self.exchange.fetch_ticker(symbol)
        
        last_price = _finite_float(ticker.get('last'), f"{symbol} 最新价")
        if last_price <= 0:
            logger.warning("无效价格数据 %s: %s", symbol, last_price)
            raise ValueError(f"{symbol} 的行情价格无效: {last_price}")
        
        return TickerData(
            symbol=symbol,
            last_price=last_price,
            high_24h=_finite_float(ticker.get('high') or last_price, f"{symbol} 最高价"),
            low_24h=_finite_float(ticker.get('low') or last_price, f"{symbol} 最低价"),
            volume_24h=_finite_float(ticker.get('quoteVolume') or 0, f"{symbol} 成交额"),
            change_24h_percent=_finite_float(
                ticker.get('percentage') or 0, f"{symbol} 涨跌幅"
            ),
            timestamp=ticker.get('timestamp') or 0
        )
    
    def fetch_tickers(self, symbols: List[str]) -> Dict[str, TickerData]:
        """
        获取多个交易对的行情数据。
        
        Args:
            symbols: 交易对列表
            
        Returns:
            Dict 映射 symbol -> TickerData
        """
        result = {}
        for symbol in symbols:
            result[symbol] = self.fetch_ticker(symbol)
        return result
    
    def fetch_order_book(self, symbol: str, depth: int = 20) -> OrderBookData:
        """
        获取订单簿并计算买卖不平衡度和挂单墙。
        
        Args:
            symbol: 交易对
            depth: 获取深度 (默认 20，用于挂单墙检测)
            
        Returns:
            OrderBookData 包含不平衡度指标和挂单墙分析
        """
        order_book = self.exchange.fetch_order_book(symbol, limit=depth)
        
        bids = self._normalize_order_levels(order_book.get('bids', [])[:depth], "买盘")
        asks = self._normalize_order_levels(order_book.get('asks', [])[:depth], "卖盘")
        
        # 计算累积挂单量
        bid_volume = sum(bid[1] for bid in bids) if bids else 0
        ask_volume = sum(ask[1] for ask in asks) if asks else 0
        total_volume = bid_volume + ask_volume
        
        # 计算不平衡度：范围 -1 (全卖) 到 +1 (全买)
        if total_volume > 0:
            imbalance = (bid_volume - ask_volume) / total_volume
        else:
            imbalance = 0.0
        
        best_bid = bids[0][0] if bids else 0
        best_ask = asks[0][0] if asks else 0
        spread = best_ask - best_bid if best_bid and best_ask else 0
        mid_price = (best_bid + best_ask) / 2 if best_bid and best_ask else 0
        
        # 检测挂单墙：找到单笔挂单量超过平均值 3 倍的价位
        bid_wall_price, bid_wall_volume = self._detect_order_wall(bids)
        ask_wall_price, ask_wall_volume = self._detect_order_wall(asks)
        
        return OrderBookData(
            bids=bids,
            asks=asks,
            bid_ask_imbalance=imbalance,
            spread=spread,
            mid_price=mid_price,
            cumulative_bid_volume=bid_volume,
            cumulative_ask_volume=ask_volume,
            bid_wall_price=bid_wall_price,
            bid_wall_volume=bid_wall_volume,
            ask_wall_price=ask_wall_price,
            ask_wall_volume=ask_wall_volume
        )

    @staticmethod
    def _normalize_order_levels(levels: List[List], label: str) -> List[List[float]]:
        """校验并规范订单簿价格与数量。"""
        normalized = []
        for index, level in enumerate(levels):
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                raise ValueError(f"{label}第 {index + 1} 档格式无效")
            price = _finite_float(level[0], f"{label}第 {index + 1} 档价格")
            quantity = _finite_float(level[1], f"{label}第 {index + 1} 档数量")
            if price <= 0 or quantity < 0:
                raise ValueError(f"{label}第 {index + 1} 档价格或数量无效")
            normalized.append([price, quantity])
        return normalized
    
    def _detect_order_wall(self, orders: List[List[float]], threshold: float = 3.0) -> tuple:
        """
        检测订单墙：单笔挂单量超过平均值 N 倍的价位。
        
        Args:
            orders: 订单列表 [[price, volume], ...]
            threshold: 判定为挂单墙的倍数阈值
            
        Returns:
            返回（挂单墙价格，挂单墙数量）或（空值，0）
        """
        if not orders or len(orders) < 3:
            return None, 0.0
        
        volumes = [o[1] for o in orders]
        avg_volume = sum(volumes) / len(volumes)
        
        # 找到最大的超过阈值的挂单
        for price, volume in orders:
            if volume >= avg_volume * threshold:
                return price, volume
        
        return None, 0.0
    
    def fetch_funding_rate(self, symbol: str) -> FundingRateData:
        """
        获取当前资金费率。
        
        Args:
            symbol: 交易对
            
        Returns:
            FundingRateData 包含当前费率和年化费率
        """
        # 使用 CCXT 统一资金费率接口
        funding_info = self.exchange.fetch_funding_rate(symbol)
        
        rate = _finite_float(funding_info.get('fundingRate', 0), "资金费率")
        next_time = int(
            _finite_float(funding_info.get('fundingTimestamp', 0), "资金费时间")
        )
        
        # 年化: 每天 3 个资金周期, 365 天
        annualized = rate * 3 * 365 * 100  # 转换为百分比
        
        return FundingRateData(
            symbol=symbol,
            funding_rate=rate,
            funding_rate_annualized=annualized,
            next_funding_time=next_time
        )
    
    def fetch_long_short_ratio(self, symbol: str) -> LongShortRatioData:
        """
        获取多空持仓比率数据。
        
        调用币安公开 API，获取全市场多空账户比和大户持仓比。
        
        Args:
            symbol: 交易对 (例如 'BTC/USDT')
            
        Returns:
            LongShortRatioData 包含多空比率数据
        """
        import time
        binance_symbol = self._raw_symbol(symbol)
        
        try:
            # 1. 获取全市场多空账户比
            global_ratio = self.exchange.fapiDataGetGlobalLongShortAccountRatio({
                'symbol': binance_symbol,
                'period': '5m',
                'limit': 1
            })
            
            if global_ratio and len(global_ratio) > 0:
                latest = global_ratio[0]
                long_account = _finite_float(latest.get('longAccount', 0.5), "多头账户占比")
                short_account = _finite_float(latest.get('shortAccount', 0.5), "空头账户占比")
                ls_ratio = _finite_float(latest.get('longShortRatio', 1.0), "多空账户比")
                timestamp = int(
                    _finite_float(
                        latest.get('timestamp', time.time() * 1000),
                        "多空比时间",
                    )
                )
            else:
                long_account = 0.5
                short_account = 0.5
                ls_ratio = 1.0
                timestamp = int(time.time() * 1000)
            
            # 2. 获取大户多空持仓比
            try:
                top_ratio = self.exchange.fapiDataGetTopLongShortPositionRatio({
                    'symbol': binance_symbol,
                    'period': '5m',
                    'limit': 1
                })
                
                if top_ratio and len(top_ratio) > 0:
                    top_latest = top_ratio[0]
                    # 兼容两种 API 响应格式: longPosition (持仓比) 或 longAccount (账户比)
                    top_long = _finite_float(
                        top_latest.get('longPosition', top_latest.get('longAccount', 0.5)),
                        "大户多头占比",
                    )
                    top_short = _finite_float(
                        top_latest.get('shortPosition', top_latest.get('shortAccount', 0.5)),
                        "大户空头占比",
                    )
                else:
                    top_long = 0.5
                    top_short = 0.5
            except Exception as e:
                logger.debug("获取大户持仓比失败: %s", e)
                top_long = 0.5
                top_short = 0.5
            
            return LongShortRatioData(
                symbol=symbol,
                long_account_ratio=long_account,
                short_account_ratio=short_account,
                long_short_ratio=ls_ratio,
                top_trader_long_ratio=top_long,
                top_trader_short_ratio=top_short,
                timestamp=timestamp
            )
            
        except Exception as e:
            logger.warning("获取多空持仓比失败 %s: %s", symbol, e)
            # 返回默认值
            return LongShortRatioData(
                symbol=symbol,
                long_account_ratio=0.5,
                short_account_ratio=0.5,
                long_short_ratio=1.0,
                top_trader_long_ratio=0.5,
                top_trader_short_ratio=0.5,
                timestamp=int(time.time() * 1000)
            )
    
    def fetch_top_gainers_losers(self, limit: int = 50) -> Dict[str, Any]:
        """
        获取涨跌幅榜用于市场广度分析。
        
        Args:
            limit: 分析的头部币种数量
            
        Returns:
            Dict 包含涨幅榜、跌幅榜和涨跌比
        """
        tickers = self.exchange.fetch_tickers()
        
        # 先按成交额选取流动性最高的 USDT 合约，再统计其涨跌分布。
        universe = []
        for symbol, data in tickers.items():
            normalized_symbol = symbol.split(':')[0]
            percentage = data.get('percentage')
            if not normalized_symbol.endswith('/USDT') or percentage is None:
                continue
            quote_volume = data.get('quoteVolume') or 0
            try:
                percentage_value = _finite_float(percentage, f"{symbol} 涨跌幅")
                volume_value = _finite_float(quote_volume, f"{symbol} 成交额")
            except ValueError:
                continue
            universe.append((normalized_symbol, percentage_value, volume_value))
        liquid_pairs = sorted(universe, key=lambda item: item[2], reverse=True)[:limit]
        top_pairs = [(symbol, percentage) for symbol, percentage, _ in liquid_pairs]
        
        # 统计前 N 个中的涨跌数量用于计算涨跌比
        gainers_in_top = [(s, p) for s, p in top_pairs if p > 0]
        losers_in_top = [(s, p) for s, p in top_pairs if p < 0]
        
        advance_count = len(gainers_in_top)
        decline_count = len(losers_in_top)
        
        if decline_count > 0:
            ad_ratio = advance_count / decline_count
        else:
            # 使用大数值代替无穷值，确保 JSON 可序列化
            ad_ratio = 9999.0 if advance_count > 0 else 1.0
        
        # 获取实际的前 10 个涨幅榜 (最正) 和前 10 个跌幅榜 (最负)
        sorted_by_gain = sorted(top_pairs, key=lambda item: item[1], reverse=True)
        top_10_gainers = sorted_by_gain[:10]
        top_10_losers = sorted(top_pairs, key=lambda item: item[1])[:10]
        
        return {
            'gainers': top_10_gainers,
            'losers': top_10_losers,
            'advance_count': advance_count,
            'decline_count': decline_count,
            'advance_decline_ratio': ad_ratio
        }
    
    # =========================================================================
    # 私有端点 (需要认证)
    # =========================================================================
    
    def _require_auth(self):
        """检查 API 凭证是否已配置。"""
        if not self.api_key or not self.api_secret:
            raise AuthenticationError("私有接口")
    
    def fetch_balance(self) -> Dict[str, float]:
        """
        获取账户余额。
        
        Returns:
            Dict 包含 USDT 余额信息
        """
        self._require_auth()
        balance = self.exchange.fetch_balance()
        
        usdt = balance.get('USDT', {})
        return {
            'total': _finite_float(usdt.get('total', 0) or 0, "账户总余额"),
            'free': _finite_float(usdt.get('free', 0) or 0, "账户可用余额"),
            'used': _finite_float(usdt.get('used', 0) or 0, "账户已用余额")
        }
    
    def fetch_positions(self, symbols: List[str] = None) -> List[Dict]:
        """
        获取当前持仓。
        
        Args:
            symbols: 可选的交易对列表，用于过滤
            
        Returns:
            持仓字典列表
        """
        self._require_auth()
        # CCXT binanceusdm 允许传递 symbols 参数来过滤 (映射到 API)
        # 注意: 即使传递了 symbols，某些交易所也可能返回所有并在本地过滤
        positions = self.exchange.fetch_positions(symbols)
        
        # 从账户 API 获取杠杆信息（因为 positionRisk 不返回 leverage）
        leverage_map = self._fetch_leverage_map()
        
        # 仅过滤活跃持仓
        active = []
        for pos in positions:
            contracts = _finite_float(pos.get('contracts', 0) or 0, "仓位数量")
            if contracts != 0:
                active.append(self._format_position(pos, leverage_map))
        
        return active
    
    def _fetch_leverage_map(self) -> Dict[str, int]:
        """
        从账户 API 获取各交易对的杠杆设置。
        
        Returns:
            Dict 交易对 -> 杠杆倍数
        """
        try:
            # 使用 CCXT 的底层方法获取账户信息
            account = self.exchange.fapiPrivateV2GetAccount()
            leverage_map = {}
            
            # 从 positions 数组中提取杠杆
            for pos in account.get('positions', []):
                symbol = pos.get('symbol', '')
                leverage = pos.get('leverage')
                if symbol and leverage:
                    # 转换为 CCXT 格式：BTCUSDT -> BTC/USDT
                    ccxt_symbol = self._binance_to_ccxt_symbol(symbol)
                    leverage_map[ccxt_symbol] = int(leverage)
            
            return leverage_map
        except Exception as e:
            # 只读查询仍可用，但持仓杠杆将标记为未知并阻止后续开仓
            logger.error("无法获取杠杆信息，开仓将被风控拒绝: %s", e)
            return {}
    
    def _binance_to_ccxt_symbol(self, binance_symbol: str) -> str:
        """将 Binance 格式 (BTCUSDT) 转换为 CCXT 格式 (BTC/USDT)。"""
        # 简单处理：假设都是 USDT 结尾
        if binance_symbol.endswith('USDT'):
            base = binance_symbol[:-4]
            return f"{base}/USDT"
        return binance_symbol
        
    def _format_position(self, pos: Dict, leverage_map: Dict[str, int] = None) -> Dict:
        """格式化单个持仓数据 (双向持仓模式)。"""
        # 移除可能的后缀，如 DOGE/USDT:USDT -> DOGE/USDT
        raw_symbol = pos['symbol']
        symbol = raw_symbol.split(':')[0]
        
        raw_contracts = _finite_float(pos.get('contracts', 0) or 0, "仓位数量")
        contracts = abs(raw_contracts)
        
        # 双向持仓模式下，使用 CCXT 返回的 side 字段判断仓位方向
        # CCXT 对于 binanceusdm 会返回 'long' 或 'short'
        raw_side = str(pos.get('side') or '').lower()
        if raw_side == 'long':
            position_side = 'LONG'
        elif raw_side == 'short':
            position_side = 'SHORT'
        else:
            # 回退到旧逻辑（单向模式兼容，理论上不应该走到这里）
            position_side = 'LONG' if raw_contracts > 0 else 'SHORT'
            logger.warning("无法从 CCXT 获取 side 字段，使用 contracts 符号判断: %s", position_side)
        
        # 杠杆取自账户接口。无法确认时保持 None，交由风控拒绝增险动作；
        # 一旦静默降级为 1，杠杆上限校验就会失效。
        leverage = leverage_map.get(symbol) if leverage_map else None
        
        return {
            'symbol': symbol,  # 标准化为基础资产/计价资产
            'side': position_side,
            'contracts': contracts,
            'notional': abs(_finite_float(pos.get('notional') or 0, "仓位名义价值")),
            'entry_price': _finite_float(pos.get('entryPrice') or 0, "开仓均价"),
            'mark_price': _finite_float(pos.get('markPrice') or 0, "标记价格"),
            'unrealized_pnl': _finite_float(
                pos.get('unrealizedPnl') or 0, "未实现盈亏"
            ),
            'percentage': _finite_float(pos.get('percentage') or 0, "仓位收益率"),
            'leverage': leverage
        }
    
    # =========================================================================
    # 工具方法
    # =========================================================================
    
    def get_precision(self, symbol: str) -> Dict[str, int]:
        """
        获取交易对的价格和数量精度。
        """
        self.load_markets()
        market = self.exchange.market(symbol)
        precision = market.get('precision', {})
        return {
            'price': precision.get('price'),
            'amount': precision.get('amount')
        }

    def amount_to_precision(self, symbol: str, value: float) -> float:
        """使用交易所市场规则格式化数量。"""
        self.load_markets()
        return _finite_float(
            self.exchange.amount_to_precision(symbol, value), f"{symbol} 数量精度结果"
        )

    def price_to_precision(self, symbol: str, value: float) -> float:
        """使用交易所市场规则格式化价格。"""
        self.load_markets()
        return _finite_float(
            self.exchange.price_to_precision(symbol, value), f"{symbol} 价格精度结果"
        )
    
    def get_fees(self, symbol: str) -> Dict[str, float]:
        """
        获取交易对的手续费率。
        
        Returns:
            Dict 包含 taker 和 maker 手续费 (小数形式，例如 0.0004 表示 0.04%)
        """
        self.load_markets()
        market = self._markets_cache.get(symbol, {})
        taker = market.get('taker')
        maker = market.get('maker')
        
        # 币安 USDT-M 永续常见默认费率 (示例值，实际以交易所返回为准)
        if taker is None:
            taker = 0.0004
        if maker is None:
            maker = 0.0002
        
        return {
            'taker': _finite_float(taker, f"{symbol} 吃单手续费"),
            'maker': _finite_float(maker, f"{symbol} 挂单手续费")
        }
    
    def truncate_to_precision(self, value: float, precision: int) -> float:
        """兼容旧调用；仅接受十进制位数语义。"""
        if not isinstance(precision, int):
            raise ValueError("精度不是十进制位数，请使用 amount_to_precision")
        multiplier = 10 ** precision
        return int(value * multiplier) / multiplier
    
    def get_min_notional(self, symbol: str) -> float:
        """获取最小名义价值。"""
        self.load_markets()
        market = self.exchange.market(symbol)
        limits = market.get('limits', {})
        cost_limits = limits.get('cost', {})
        minimum = cost_limits.get('min')
        if minimum is None:
            raise ValueError(f"交易所未返回 {symbol} 的最小名义价值")
        minimum_value = _finite_float(minimum, f"{symbol} 最小名义价值")
        if minimum_value <= 0:
            raise ValueError(f"{symbol} 的最小名义价值必须大于 0")
        return minimum_value
    
    def calculate_quantity(self, symbol: str, usdt_amount: float, current_price: float = None) -> float:
        """计算下单数量。"""
        if current_price is None:
            ticker = self.fetch_ticker(symbol)
            current_price = ticker.last_price
        
        current_price = _finite_float(current_price, f"{symbol} 当前价格")
        usdt_amount = _finite_float(usdt_amount, "下单金额")
        if current_price <= 0:
            raise ValueError(f"{symbol} 的价格无效: {current_price}")
        if usdt_amount <= 0:
            raise ValueError("下单金额必须大于 0")
        
        raw_quantity = usdt_amount / current_price
        return self.amount_to_precision(symbol, raw_quantity)
    
    def get_position_size(self, symbol: str, position_side: str = None) -> dict:
        """
        获取交易对的当前持仓大小。
        
        包含重试机制以应对 API 延迟。
        
        Args:
            symbol: 交易对
            
        Returns:
            Dict 包含合约数、方向和名义价值
        """
        self._require_auth()
        
        max_retries = 3
        
        for i in range(max_retries):
            # 尝试指定 symbol 获取，此方式在部分 API 上更精确
            try:
                positions = [
                    pos for pos in self.fetch_positions([symbol])
                    if pos['symbol'] == symbol
                    and (not position_side or pos['side'] == position_side.upper())
                ]
                if len(positions) > 1:
                    raise ValueError(f"{symbol} 同时存在双向仓位，必须明确指定 side")
                if positions:
                    return positions[0]
            except ValueError:
                raise
            except Exception as e:
                logger.warning("尝试获取持仓失败 (%d/%d): %s", i+1, max_retries, e)
            
            # 如果没找到，但在前几次重试中，稍微等待一下
            if i < max_retries - 1:
                time.sleep(1)
        
        return None
    
    # =========================================================================
    # 交易执行 (需要认证)
    # =========================================================================
    
    def create_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        position_side: str,
        client_order_id: str = None
    ) -> Dict:
        """
        创建市价单 (双向持仓模式)。
        
        Args:
            symbol: 交易对 (例如 'BTC/USDT')
            side: 'buy' (买) 或 'sell' (卖)
            quantity: 订单数量 (基础货币)
            position_side: 持仓方向 'LONG' 或 'SHORT' (必需)
            
        Returns:
            交易所的订单响应
        """
        self._require_auth()
        
        logger.info(
            "正在创建市价单: %s %s %.8f (positionSide=%s)",
            side.upper(), symbol, quantity, position_side
        )
        
        params = {
            'positionSide': position_side.upper(),
        }
        if client_order_id:
            params['newClientOrderId'] = client_order_id

        try:
            order = self.exchange.create_order(
                symbol=symbol,
                type='market',
                side=side.lower(),
                amount=quantity,
                params=params
            )
        except (ccxt.RequestTimeout, ccxt.NetworkError) as exc:
            reconciled = self.fetch_order_by_client_id(symbol, client_order_id)
            if reconciled is None:
                raise OrderResultUnknownError(
                    symbol, side, client_order_id or '未提供', str(exc)
                ) from exc
            order = reconciled
        
        logger.info("订单已创建: %s", order.get('id'))
        return order

    def fetch_order_by_client_id(self, symbol: str, client_order_id: str) -> Optional[Dict]:
        """按客户端订单 ID 查询结果，用于消解网络超时歧义。"""
        if not client_order_id:
            return None
        try:
            raw = self.exchange.fapiPrivateGetOrder({
                'symbol': self._raw_symbol(symbol),
                'origClientOrderId': client_order_id,
            })
            market = self.exchange.market(symbol)
            return self.exchange.parse_order(raw, market)
        except Exception as exc:
            logger.error("订单对账失败 %s: %s", client_order_id, exc)
            return None
    
    def create_stop_loss_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        stop_price: float,
        position_side: str,
        client_order_id: str = None
    ) -> Dict:
        """
        创建市价止损单 (双向持仓模式)。
        
        多头持仓: side='sell', position_side='LONG', 当价格跌至 stop_price 时触发
        空头持仓: side='buy', position_side='SHORT', 当价格涨至 stop_price 时触发
        
        Args:
            symbol: 交易对
            side: 'buy' 或 'sell' (与持仓方向相反)
            quantity: 订单数量
            stop_price: 触发价格
            position_side: 持仓方向 'LONG' 或 'SHORT' (必需)
            
        Returns:
            交易所的订单响应
        """
        self._require_auth()
        
        stop_price = self.price_to_precision(symbol, stop_price)
        
        logger.info(
            "正在创建止损单: %s %s %.8f @ %.2f (positionSide=%s)",
            side.upper(), symbol, quantity, stop_price, position_side
        )
        
        # 使用币安的 STOP_MARKET 订单类型 (双向持仓模式)
        params = {
            'stopPrice': stop_price,
            'positionSide': position_side.upper(),
        }
        if client_order_id:
            params['newClientOrderId'] = client_order_id

        try:
            order = self.exchange.create_order(
                symbol=symbol,
                type='STOP_MARKET',
                side=side.lower(),
                amount=quantity,
                params=params
            )
        except (ccxt.RequestTimeout, ccxt.NetworkError) as exc:
            order = self.fetch_conditional_by_client_id(symbol, client_order_id)
            if order is None:
                raise OrderResultUnknownError(
                    symbol, side, client_order_id or '未提供', str(exc)
                ) from exc
        
        logger.info("止损单已创建: %s", order.get('id'))
        return order

    def fetch_conditional_by_client_id(
        self, symbol: str, client_order_id: str
    ) -> Optional[Dict]:
        """查询普通或算法条件单，消解创建超时。"""
        normal = self.fetch_order_by_client_id(symbol, client_order_id)
        if normal is not None:
            return normal
        if not client_order_id:
            return None
        try:
            orders = self._get_open_algo_orders(symbol)
            for order in orders:
                candidate = order.get('clientAlgoId') or order.get('clientOrderId')
                if candidate == client_order_id:
                    algo_id = order.get('algoId')
                    if algo_id is None:
                        raise ValueError("算法订单响应缺少 algoId")
                    return {
                        'id': str(algo_id),
                        'clientOrderId': client_order_id,
                        'symbol': symbol,
                        'type': order.get('orderType'),
                        'side': str(order.get('side', '')).lower(),
                        'amount': _finite_float(order.get('quantity', 0), "算法订单数量"),
                        'stopPrice': _finite_float(
                            order.get('triggerPrice', 0), "算法订单触发价"
                        ),
                        'status': str(order.get('algoStatus', '')).lower(),
                        'is_algo': True,
                        'info': order,
                    }
        except Exception as exc:
            logger.error("条件单对账失败 %s: %s", client_order_id, exc)
        return None

    @staticmethod
    def _raw_symbol(symbol: str) -> str:
        return symbol.split(':')[0].replace('/', '')

    def _call_algo_api(self, operation: str, params: Dict) -> object:
        """兼容不同 CCXT 版本的币安合约算法订单端点。"""
        endpoints = {
            "get_open": (
                "fapiPrivateGetOpenAlgoOrders",
                "openAlgoOrders",
                "GET",
            ),
            "delete_order": (
                "fapiPrivateDeleteAlgoOrder",
                "algoOrder",
                "DELETE",
            ),
            "delete_all": (
                "fapiPrivateDeleteAlgoOpenOrders",
                "algoOpenOrders",
                "DELETE",
            ),
        }
        method_name, path, http_method = endpoints[operation]
        generated_method = getattr(self.exchange, method_name, None)
        if callable(generated_method):
            return generated_method(params)
        return self.exchange.request(path, "fapiPrivate", http_method, params)

    @staticmethod
    def _unwrap_algo_orders(response: object) -> List[Dict]:
        """把 Binance 新旧算法订单响应统一为订单列表。"""
        if isinstance(response, list):
            return response
        if not isinstance(response, dict):
            return []
        data = response.get("data", response)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("orders", "rows", "list"):
                orders = data.get(key)
                if isinstance(orders, list):
                    return orders
        return []

    def _get_open_algo_orders(self, symbol: str) -> List[Dict]:
        response = self._call_algo_api(
            "get_open", {"symbol": self._raw_symbol(symbol)}
        )
        return self._unwrap_algo_orders(response)

    def _delete_algo_order(self, symbol: str, order_id: str) -> object:
        return self._call_algo_api(
            "delete_order",
            {"symbol": self._raw_symbol(symbol), "algoId": order_id},
        )

    def _delete_all_open_algo_orders(self, symbol: str) -> object:
        return self._call_algo_api(
            "delete_all", {"symbol": self._raw_symbol(symbol)}
        )
    
    def cancel_all_orders(self, symbol: str) -> List[Dict]:
        """
        取消交易对的所有挂单（包括普通订单和算法订单/条件委托单）。
        
        币安合约中止损/止盈单被创建为算法订单 (algoType: CONDITIONAL)，
        需要使用专门的 API 来取消。
        
        Args:
            symbol: 交易对
            
        Returns:
            已取消订单列表
        """
        self._require_auth()
        cancelled_orders = []
        binance_symbol = self._raw_symbol(symbol)
        errors = []
        
        # 1. 取消普通订单
        try:
            result = self.exchange.fapiPrivateDeleteAllOpenOrders({
                'symbol': binance_symbol
            })
            logger.info("已取消 %s 的普通订单: %s", symbol, result)
            if isinstance(result, list):
                cancelled_orders.extend(result)
        except Exception as e:
            logger.warning("取消普通订单失败: %s", e)
            # 回退到 CCXT 方法
            try:
                result = self.exchange.cancel_all_orders(symbol)
                if isinstance(result, list):
                    cancelled_orders.extend(result)
            except Exception as e2:
                logger.warning("CCXT cancel_all_orders 也失败: %s", e2)
                errors.append(f"普通订单撤销失败: {e2}")
        
        # 2. 取消算法订单（止损/止盈条件委托单）
        try:
            result = self._delete_all_open_algo_orders(symbol)
            logger.info("已取消 %s 的算法订单: %s", symbol, result)
        except Exception as e:
            logger.warning("取消算法订单失败: %s", e)
            # 尝试逐个取消
            try:
                algo_orders = self._get_open_algo_orders(symbol)
                for order in algo_orders:
                    try:
                        algo_id = order.get('algoId')
                        if algo_id is None:
                            raise ValueError("算法订单响应缺少 algoId")
                        self._delete_algo_order(symbol, str(algo_id))
                        cancelled_orders.append(order)
                        logger.info("已取消算法订单: %s", algo_id)
                    except Exception as inner_e:
                        logger.warning("取消算法订单 %s 失败: %s", order.get('algoId'), inner_e)
                        errors.append(
                            f"算法订单 {order.get('algoId')} 撤销失败: {inner_e}"
                        )
            except Exception as e2:
                logger.warning("获取算法订单也失败: %s", e2)
                errors.append(f"算法订单撤销失败: {e2}")

        remaining = self.get_open_orders(symbol)
        if remaining:
            raise RuntimeError(f"撤单后仍有 {len(remaining)} 个挂单")
        if errors:
            raise RuntimeError("；".join(errors))
        return cancelled_orders
    
    def cancel_order_by_id(self, symbol: str, order_id: str) -> Dict:
        """
        根据订单 ID 取消单个订单。
        
        自动检测订单类型（普通订单或算法订单）并调用相应的 API。
        
        Args:
            symbol: 交易对 (例如 'BTC/USDT')
            order_id: 订单 ID
            
        Returns:
            取消结果
        """
        self._require_auth()
        
        logger.info("正在取消订单: symbol=%s, order_id=%s", symbol, order_id)
        
        # 先尝试作为普通订单取消
        try:
            result = self.exchange.cancel_order(order_id, symbol)
            logger.info("已取消普通订单: %s", order_id)
            return {'success': True, 'order_id': order_id, 'type': 'normal', 'result': result}
        except (ccxt.RequestTimeout, ccxt.NetworkError) as exc:
            # 网络异常下撤单结果不明，不能改用 algoId 重试：
            # orderId 与 algoId 属于不同 ID 空间，可能撤销无关订单。
            raise OrderResultUnknownError(
                symbol, 'CANCEL', order_id, str(exc)
            ) from exc
        except Exception as e:
            logger.debug("普通订单撤销未成功，改按算法订单撤销: %s", e)
        
        # 尝试作为算法订单取消 (使用 algoId)
        try:
            result = self._delete_algo_order(symbol, order_id)
            logger.info("已取消算法订单: %s", order_id)
            return {'success': True, 'order_id': order_id, 'type': 'algo', 'result': result}
        except (ccxt.RequestTimeout, ccxt.NetworkError) as exc:
            raise OrderResultUnknownError(
                symbol, 'CANCEL', order_id, str(exc)
            ) from exc
        except Exception as e:
            logger.error("取消订单失败 %s: %s", order_id, e)
            return {'success': False, 'order_id': order_id, 'error': str(e)}
    
    # =========================================================================
    # 高级交易功能（杠杆、保证金模式、止盈止损）
    # =========================================================================
    
    def set_leverage(self, symbol: str, leverage: int) -> Dict:
        """
        设置交易对杠杆。
        
        Args:
            symbol: 交易对 (例如 'BTC/USDT')
            leverage: 杠杆倍数 (1-125)
            
        Returns:
            交易所响应
        """
        self._require_auth()
        
        if not 1 <= leverage <= 125:
            raise ValueError("杠杆必须在 1 到 125 之间")
        
        logger.info("正在设置 %s 杠杆为 %dx", symbol, leverage)
        
        try:
            result = self.exchange.set_leverage(leverage, symbol)
            logger.info("杠杆已设置: %s -> %dx", symbol, leverage)
            return result
        except Exception as e:
            logger.error("设置杠杆失败 %s: %s", symbol, e)
            raise
    
    def set_margin_mode(self, symbol: str, mode: str) -> Dict:
        """
        设置交易对保证金模式。
        
        Args:
            symbol: 交易对
            mode: 'cross' (全仓) 或 'isolated' (逐仓)
            
        Returns:
            交易所响应
        """
        self._require_auth()
        
        mode = mode.lower()
        if mode not in ('cross', 'isolated'):
            raise ValueError(f"无效的保证金模式: {mode}")
        
        logger.info("正在设置 %s 保证金模式为 %s", symbol, mode)
        
        try:
            result = self.exchange.set_margin_mode(mode, symbol)
            logger.info("保证金模式已设置: %s -> %s", symbol, mode)
            return result
        except Exception as e:
            # 如果已经是该模式，币安会返回错误，这是可以忽略的
            if 'No need to change margin type' in str(e):
                logger.info("保证金模式已经是 %s，无需更改", mode)
                return {'info': 'already_set'}
            logger.error("设置保证金模式失败 %s: %s", symbol, e)
            raise
    
    def create_take_profit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        take_profit_price: float,
        position_side: str,
        client_order_id: str = None
    ) -> Dict:
        """
        创建市价止盈单 (双向持仓模式)。
        
        多头持仓: side='sell', position_side='LONG', 当价格涨至 take_profit_price 时触发
        空头持仓: side='buy', position_side='SHORT', 当价格跌至 take_profit_price 时触发
        
        Args:
            symbol: 交易对
            side: 'buy' 或 'sell' (与持仓方向相反)
            quantity: 订单数量
            take_profit_price: 触发价格
            position_side: 持仓方向 'LONG' 或 'SHORT' (必需)
            
        Returns:
            交易所的订单响应
        """
        self._require_auth()
        
        take_profit_price = self.price_to_precision(symbol, take_profit_price)
        
        logger.info(
            "正在创建止盈单: %s %s %.8f @ %.2f (positionSide=%s)",
            side.upper(), symbol, quantity, take_profit_price, position_side
        )
        
        # 使用币安的 TAKE_PROFIT_MARKET 订单类型 (双向持仓模式)
        params = {
            'stopPrice': take_profit_price,
            'positionSide': position_side.upper(),
        }
        if client_order_id:
            params['newClientOrderId'] = client_order_id

        try:
            order = self.exchange.create_order(
                symbol=symbol,
                type='TAKE_PROFIT_MARKET',
                side=side.lower(),
                amount=quantity,
                params=params
            )
        except (ccxt.RequestTimeout, ccxt.NetworkError) as exc:
            order = self.fetch_conditional_by_client_id(symbol, client_order_id)
            if order is None:
                raise OrderResultUnknownError(
                    symbol, side, client_order_id or '未提供', str(exc)
                ) from exc
        
        logger.info("止盈单已创建: %s", order.get('id'))
        return order
    
    def get_open_orders(self, symbol: str = None) -> List[Dict]:
        """
        获取交易对或所有交易对的挂单，包括条件委托单（止损/止盈）。
        
        Args:
            symbol: 交易对 (可选, None 表示所有)
            
        Returns:
            挂单列表（包括普通订单和条件订单）
        """
        self._require_auth()
        
        all_orders = []
        errors = []
        
        try:
            # 获取普通挂单
            if symbol:
                orders = self.exchange.fetch_open_orders(symbol)
            else:
                orders = self.exchange.fetch_open_orders()
            all_orders.extend(orders)
        except Exception as e:
            logger.warning("获取普通挂单失败: %s", e)
            errors.append(f"普通挂单查询失败: {e}")
        
        # 获取算法订单（条件委托单：止损/止盈）
        # 算法订单需要使用专用的 API 端点
        try:
            configured_symbols = getattr(self, "trading_symbols", None)
            if configured_symbols is None:
                configured_symbols = get_config().TRADING_SYMBOLS
            algo_symbols = [symbol] if symbol else list(configured_symbols)
            for algo_symbol in algo_symbols:
                algo_orders = self._get_open_algo_orders(algo_symbol)
                for order in algo_orders:
                    algo_id = order.get('algoId')
                    if algo_id is None:
                        raise ValueError("算法订单响应缺少 algoId")
                    order_id = str(algo_id)
                    already_present = any(
                        str((item.get('info') or {}).get('algoId') or '') == order_id
                        and str(item.get('symbol') or '').split(':')[0]
                        == algo_symbol.split(':')[0]
                        for item in all_orders
                    )
                    if not already_present:
                        all_orders.append({
                            'id': order_id,
                            'symbol': algo_symbol,
                            'type': order.get('orderType'),  # STOP_MARKET, TAKE_PROFIT_MARKET
                            'side': order.get('side'),
                            'positionSide': order.get('positionSide'),
                            'amount': _finite_float(
                                order.get('quantity', 0), "算法订单数量"
                            ),
                            'stopPrice': _finite_float(
                                order.get('triggerPrice', 0), "算法订单触发价"
                            ),
                            'status': order.get('algoStatus'),
                            'is_algo': True,
                            'info': order
                        })
        except Exception as e:
            logger.warning("获取算法订单失败: %s", e)
            errors.append(f"算法挂单查询失败: {e}")

        if errors:
            raise RuntimeError("；".join(errors))
        return all_orders

    def cancel_position_orders(self, symbol: str, position_side: str) -> List[Dict]:
        """只撤销指定持仓方向的保护单。"""
        cancelled = []
        failures = []
        for order in self.get_open_orders(symbol):
            raw_side = str(
                order.get('positionSide')
                or (order.get('info') or {}).get('positionSide')
                or ''
            ).upper()
            if not raw_side:
                failures.append(f"订单 {order.get('id')} 缺少持仓方向")
                continue
            if raw_side != position_side.upper():
                continue
            order_type = str(order.get('type', '')).upper()
            if 'STOP' not in order_type and 'TAKE_PROFIT' not in order_type:
                continue
            result = self.cancel_order_by_id(symbol, str(order['id']))
            if result.get('success'):
                cancelled.append(order)
            else:
                failures.append(result.get('error', str(order['id'])))
        if failures:
            raise RuntimeError("；".join(failures))
        return cancelled
    
    def cancel_orders_by_type(self, symbol: str, order_type: str) -> List[Dict]:
        """
        取消特定类型的订单。
        
        Args:
            symbol: 交易对
            order_type: 'stop_loss', 'take_profit', 或 'all'
            
        Returns:
            已取消订单列表
        """
        self._require_auth()
        
        if order_type.lower() == 'all':
            return self.cancel_all_orders(symbol)

        if order_type.lower() not in {'stop_loss', 'take_profit'}:
            raise ValueError(f"不支持的订单类型: {order_type}")
        
        # 获取挂单并按类型过滤
        orders = self.get_open_orders(symbol)
        cancelled = []
        
        # 匹配模式：同时检查 CCXT type 和币安原始 info.type
        # 止损单类型: STOP_MARKET, STOP, stop_market
        # 止盈单类型: TAKE_PROFIT_MARKET, TAKE_PROFIT, take_profit_market
        type_patterns = {
            'stop_loss': ['STOP_MARKET', 'STOP', 'stop_market', 'stop'],
            'take_profit': ['TAKE_PROFIT_MARKET', 'TAKE_PROFIT', 'take_profit_market', 'take_profit']
        }
        
        target_patterns = type_patterns.get(order_type.lower(), [])
        
        for order in orders:
            # 获取订单类型 (检查多个来源)
            ccxt_type = str(order.get('type', '')).upper()
            info_type = str(order.get('info', {}).get('type', '')).upper() if order.get('info') else ''
            
            # 匹配任一来源
            matched = False
            for pattern in target_patterns:
                if ccxt_type == pattern.upper() or info_type == pattern.upper():
                    matched = True
                    break
            
            if matched:
                try:
                    # 根据订单类型选择正确的取消 API
                    if order.get('is_algo'):
                        # 算法订单需要使用 algoId 取消
                        self._delete_algo_order(symbol, str(order['id']))
                    else:
                        # 普通订单使用标准 CCXT 方法
                        self.exchange.cancel_order(order['id'], symbol)
                    cancelled.append(order)
                    logger.info("已取消 %s 订单: %s (type=%s, is_algo=%s)", 
                               order_type, order['id'], ccxt_type, order.get('is_algo', False))
                except Exception as e:
                    logger.warning("取消订单失败 %s: %s", order['id'], e)
                    raise RuntimeError(f"取消订单失败 {order['id']}: {e}") from e
            else:
                logger.debug("订单 %s 类型不匹配 (type=%s, info.type=%s), 跳过",
                            order['id'], ccxt_type, info_type)
        
        return cancelled

