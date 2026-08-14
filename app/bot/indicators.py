"""
技术指标计算模块。

提供各种技术分析指标的计算功能，使用纯 pandas/numpy 实现以获得最大兼容性。
"""

import pandas as pd
import numpy as np
import logging
from typing import List, Tuple, Optional
from dataclasses import dataclass
from app.bot.exceptions import InsufficientDataError
from app.bot.tz_utils import from_timestamp, format_time

logger = logging.getLogger(__name__)


def format_price(value: float) -> str:
    """按数量级选择小数位，避免低价币被格式化成 0.00。

    Args:
        value: 价格

    Returns:
        千位分隔的价格字符串
    """
    magnitude = abs(value)
    if magnitude >= 100:
        decimals = 2
    elif magnitude >= 1:
        decimals = 4
    elif magnitude >= 0.01:
        decimals = 6
    else:
        decimals = 8
    return f"{value:,.{decimals}f}"


def calc_sma(data: List[float], period: int) -> List[Optional[float]]:
    """
    计算简单移动平均 (模块化法则: 提取为可复用函数)。
    
    Args:
        data: 价格数据列表
        period: 计算周期
        
    Returns:
        SMA 值列表，前 period-1 个位置为 None
    """
    if period <= 0:
        raise ValueError("移动平均周期必须大于 0")
    if len(data) < period:
        return [None] * len(data)
    result = [None] * (period - 1)
    for i in range(period - 1, len(data)):
        result.append(sum(data[i - period + 1:i + 1]) / period)
    return result


@dataclass
class BollingerBandsData:
    """布林带指标数据。"""
    upper: float
    middle: float
    lower: float
    bandwidth: float  # (Upper - Lower) / Middle
    percent_b: float  # (Price - Lower) / (Upper - Lower)
    is_squeeze: bool  # Bandwidth < 20-period avg


@dataclass
class TrendData:
    """趋势分析数据。"""
    ema_20: float
    ema_50: float
    ema_200: float
    trend_direction: str  # "BULLISH", "BEARISH", "NEUTRAL"
    trend_strength: str  # "STRONG", "MODERATE", "WEAK"


@dataclass
class SupportResistanceData:
    """支撑位和阻力位。"""
    supports: List[float]
    resistances: List[float]
    nearest_support: float
    nearest_resistance: float


@dataclass
class DivergenceData:
    """RSI 背离检测结果。"""
    rsi_value: float
    has_bullish_divergence: bool
    has_bearish_divergence: bool
    divergence_type: str  # "BULLISH", "BEARISH", "NONE"


@dataclass
class IndicatorSummary:
    """单个代码的完整指标摘要。"""
    symbol: str
    current_price: float
    vwap: float
    price_vs_vwap: str  # "ABOVE", "BELOW"
    trend: TrendData
    bollinger: BollingerBandsData
    atr: float
    atr_percent: float  # ATR 占价格的百分比
    rsi: float
    rsi_condition: str  # RSI 状态枚举
    divergence: DivergenceData
    support_resistance: SupportResistanceData


def create_dataframe(ohlcv_data: List[List]) -> pd.DataFrame:
    """
    将 OHLCV 列表转换为 pandas DataFrame。
    
    Args:
        ohlcv_data: 列表 [timestamp, open, high, low, close, volume]
        
    Returns:
        具有正确列名和 datetime 索引的 DataFrame
    """
    if not ohlcv_data:
        raise ValueError("OHLCV 数据为空")
    if any(len(candle) < 6 for candle in ohlcv_data):
        raise ValueError("OHLCV 数据列不完整")
    df = pd.DataFrame(
        ohlcv_data,
        columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
    )
    for column in ['timestamp', 'open', 'high', 'low', 'close', 'volume']:
        df[column] = pd.to_numeric(df[column], errors='raise')
    if not np.isfinite(
        df[['timestamp', 'open', 'high', 'low', 'close', 'volume']].to_numpy()
    ).all():
        raise ValueError("OHLCV 包含非有限数值")
    if (df['timestamp'] < 0).any():
        raise ValueError("OHLCV 时间戳不能为负数")
    if (df[['open', 'high', 'low', 'close']] <= 0).any().any() or (df['volume'] < 0).any():
        raise ValueError("OHLCV 包含非法价格或成交量")
    if ((df['high'] < df[['open', 'close', 'low']].max(axis=1)) |
            (df['low'] > df[['open', 'close', 'high']].min(axis=1))).any():
        raise ValueError("OHLCV 高低价关系非法")
    df = df.drop_duplicates('timestamp', keep='last').sort_values('timestamp')
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    return df


def _ema(series: pd.Series, period: int) -> pd.Series:
    """计算指数移动平均线 (EMA)。"""
    return series.ewm(span=period, adjust=False).mean()


def _sma(series: pd.Series, period: int) -> pd.Series:
    """计算简单移动平均线 (SMA)。"""
    return series.rolling(window=period).mean()


def _macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    计算 MACD 指标。
    
    Args:
        series: 价格序列
        fast: 快线周期 (默认 12)
        slow: 慢线周期 (默认 26)
        signal: 信号线周期 (默认 9)
        
    Returns:
        Tuple of (MACD 线, 信号线, 柱状图)
    """
    ema_fast = _ema(series, fast)
    ema_slow = _ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    计算相对强弱指数 (RSI)。
    
    使用 Wilder 平滑法 (alpha=1/period 的 EMA)。
    """
    delta = series.diff()
    
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    
    # Wilder 平滑法 (等同于 alpha=1/period 的 EMA)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))

    warmed_up = avg_gain.notna() & avg_loss.notna()
    rsi = rsi.mask(warmed_up & (avg_loss == 0) & (avg_gain > 0), 100)
    rsi = rsi.mask(warmed_up & (avg_gain == 0) & (avg_loss > 0), 0)
    rsi = rsi.mask(warmed_up & (avg_gain == 0) & (avg_loss == 0), 50)
    
    return rsi


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """计算真实波幅 (True Range)。"""
    prev_close = close.shift(1)
    
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """计算平均真实波幅 (ATR)。"""
    tr = _true_range(high, low, close)
    return tr.ewm(alpha=1/period, min_periods=period).mean()


def _bollinger_bands(series: pd.Series, period: int = 20, std_dev: float = 2.0) -> tuple:
    """
    计算布林带。
    
    Returns: (upper, middle, lower) 作为 pd.Series
    """
    middle = _sma(series, period)
    std = series.rolling(window=period).std()
    
    upper = middle + (std * std_dev)
    lower = middle - (std * std_dev)
    
    return upper, middle, lower


def _vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """
    计算成交量加权平均价格 (VWAP)。
    
    对于加密货币 (7x24)，计算从数据开始的累积 VWAP。
    """
    typical_price = (high + low + close) / 3
    cumvol = volume.cumsum()
    # 避免除零：将 0 替换为 NaN
    cumvol = cumvol.replace(0, np.nan)
    vwap = (typical_price * volume).cumsum() / cumvol
    return vwap


def calculate_emas(
    df: pd.DataFrame, periods: Optional[List[int]] = None
) -> TrendData:
    """
    计算 EMA 并确定趋势方向。
    
    Args:
        df: OHLCV DataFrame
        periods: 要计算的 EMA 周期
        
    Returns:
        TrendData 包含 EMA 和趋势评估
    """
    periods = periods or [20, 50, 200]
    current_price = df['close'].iloc[-1]
    
    ema_values = {}
    for period in periods:
        ema = _ema(df['close'], period)
        ema_values[period] = ema.iloc[-1] if len(ema) > 0 else current_price
    
    ema_20 = ema_values.get(20, current_price)
    ema_50 = ema_values.get(50, current_price)
    ema_200 = ema_values.get(200, current_price)
    
    # 确定趋势方向
    if current_price > ema_20 > ema_50 > ema_200:
        direction = "BULLISH"
        strength = "STRONG"
    elif current_price > ema_50 > ema_200:
        direction = "BULLISH"
        strength = "MODERATE"
    elif current_price > ema_200:
        direction = "BULLISH"
        strength = "WEAK"
    elif current_price < ema_20 < ema_50 < ema_200:
        direction = "BEARISH"
        strength = "STRONG"
    elif current_price < ema_50 < ema_200:
        direction = "BEARISH"
        strength = "MODERATE"
    elif current_price < ema_200:
        direction = "BEARISH"
        strength = "WEAK"
    else:
        direction = "NEUTRAL"
        strength = "WEAK"
    
    return TrendData(
        ema_20=ema_20,
        ema_50=ema_50,
        ema_200=ema_200,
        trend_direction=direction,
        trend_strength=strength
    )


def calculate_bollinger_bands(
    df: pd.DataFrame, 
    length: int = 20, 
    std: float = 2.0
) -> BollingerBandsData:
    """
    计算带有挤压检测的布林带。
    
    Args:
        df: OHLCV DataFrame
        length: 移动平均周期
        std: 标准差倍数
        
    Returns:
        BollingerBandsData 包含布林带值和分析
    """
    upper, middle, lower = _bollinger_bands(df['close'], length, std)
    
    current_price = df['close'].iloc[-1]
    upper_val = upper.iloc[-1]
    middle_val = middle.iloc[-1]
    lower_val = lower.iloc[-1]
    
    # 处理 NaN 值
    if pd.isna(upper_val) or pd.isna(lower_val):
        return BollingerBandsData(
            upper=current_price,
            middle=current_price,
            lower=current_price,
            bandwidth=0,
            percent_b=0.5,
            is_squeeze=False
        )
    
    bandwidth = (upper_val - lower_val) / middle_val if middle_val != 0 else 0
    percent_b = (current_price - lower_val) / (upper_val - lower_val) if (upper_val - lower_val) != 0 else 0.5
    
    # 检测挤压：当前带宽 < 20周期平均带宽
    bandwidth_series = (upper - lower) / middle
    avg_bandwidth = bandwidth_series.rolling(20).mean().iloc[-1]
    is_squeeze = bandwidth < avg_bandwidth * 0.8 if not pd.isna(avg_bandwidth) else False
    
    return BollingerBandsData(
        upper=upper_val,
        middle=middle_val,
        lower=lower_val,
        bandwidth=bandwidth,
        percent_b=percent_b,
        is_squeeze=is_squeeze
    )


def calculate_atr(df: pd.DataFrame, length: int = 14) -> Tuple[float, float]:
    """
    计算平均真实波幅 (ATR)。
    
    Args:
        df: OHLCV DataFrame
        length: ATR 周期
        
    Returns:
        Tuple of (ATR 值, ATR 占价格百分比)
    """
    atr_series = _atr(df['high'], df['low'], df['close'], length)
    
    atr_value = atr_series.iloc[-1]
    if pd.isna(atr_value):
        return 0.0, 0.0
    
    current_price = df['close'].iloc[-1]
    atr_percent = (atr_value / current_price) * 100 if current_price != 0 else 0
    
    return atr_value, atr_percent


def calculate_rsi(df: pd.DataFrame, length: int = 14) -> Tuple[float, str]:
    """
    计算 RSI 并评估状态。
    
    Args:
        df: OHLCV DataFrame
        length: RSI 周期
        
    Returns:
        Tuple of (RSI 值, 状态字符串)
    """
    rsi_series = _rsi(df['close'], length)
    
    rsi_value = rsi_series.iloc[-1]
    if pd.isna(rsi_value):
        logger.debug("%s RSI 计算返回 NaN，使用默认值 50", 'RSI')
        return 50.0, "NEUTRAL"
    
    if rsi_value >= 70:
        condition = "OVERBOUGHT"
    elif rsi_value <= 30:
        condition = "OVERSOLD"
    else:
        condition = "NEUTRAL"
    
    return rsi_value, condition


def calculate_vwap(df: pd.DataFrame) -> float:
    """
    计算成交量加权平均价格 (VWAP)。
    
    Args:
        df: OHLCV DataFrame
        
    Returns:
        VWAP 值
    """
    vwap_series = _vwap(df['high'], df['low'], df['close'], df['volume'])
    
    vwap_value = vwap_series.iloc[-1]
    if pd.isna(vwap_value):
        logger.debug("VWAP 计算返回 NaN，使用收盘价均值")
        return df['close'].mean()
    
    return vwap_value


def detect_support_resistance(
    df: pd.DataFrame, 
    window: int = 5, 
    num_levels: int = 3
) -> SupportResistanceData:
    """
    使用分形方法检测支撑和阻力位。
    
    Args:
        df: OHLCV DataFrame
        window: 分形检测的回溯窗口
        num_levels: 返回的层级数量
        
    Returns:
        SupportResistanceData 包含关键位
    """
    highs = df['high'].values
    lows = df['low'].values
    current_price = df['close'].iloc[-1]
    
    resistances = []
    supports = []
    
    # 寻找分形高点 (阻力) 和低点 (支撑)
    for i in range(window, len(df) - window):
        # 分形高点：高于周围的 K 线
        if highs[i] == max(highs[i-window:i+window+1]):
            resistances.append(highs[i])
        
        # 分形低点：低于周围的 K 线
        if lows[i] == min(lows[i-window:i+window+1]):
            supports.append(lows[i])
    
    # 排序并去重 (聚集附近的层级)
    resistances = sorted(set(resistances))  # 升序排列
    supports = sorted(set(supports), reverse=True)  # 降序排列
    
    # 过滤：只有当前价格上方的才是阻力位，下方的才是支撑位
    # 阻力位：升序排列后过滤，前 N 个就是最近的
    resistances = [r for r in resistances if r > current_price][:num_levels]
    
    # 支撑位：降序排列后过滤，前 N 个就是最近的
    supports = [s for s in supports if s < current_price][:num_levels]
    
    nearest_resistance = resistances[0] if resistances else current_price * 1.05
    nearest_support = supports[0] if supports else current_price * 0.95
    
    return SupportResistanceData(
        supports=supports,
        resistances=resistances,
        nearest_support=nearest_support,
        nearest_resistance=nearest_resistance
    )


def detect_divergence(df: pd.DataFrame, lookback: int = 14) -> DivergenceData:
    """
    检测 RSI 背离。
    
    看涨背离：价格创新低，RSI 低点抬高
    看跌背离：价格创新高，RSI 高点降低
    
    Args:
        df: OHLCV DataFrame
        lookback: 检查背离的回溯周期
        
    Returns:
        DivergenceData 包含背离评估
    """
    rsi_series = _rsi(df['close'], 14)
    
    if len(rsi_series) < lookback + 5:
        return DivergenceData(
            rsi_value=50.0,
            has_bullish_divergence=False,
            has_bearish_divergence=False,
            divergence_type="NONE"
        )
    
    rsi_value = rsi_series.iloc[-1]
    if pd.isna(rsi_value):
        rsi_value = 50.0
    
    # 获取近期价格和 RSI 数据
    recent_close = df['close'].iloc[-lookback:]
    recent_rsi = rsi_series.iloc[-lookback:]

    high_indices = [
        index for index in range(1, len(recent_close) - 1)
        if recent_close.iloc[index] > recent_close.iloc[index - 1]
        and recent_close.iloc[index] >= recent_close.iloc[index + 1]
    ]
    low_indices = [
        index for index in range(1, len(recent_close) - 1)
        if recent_close.iloc[index] < recent_close.iloc[index - 1]
        and recent_close.iloc[index] <= recent_close.iloc[index + 1]
    ]

    has_bearish = False
    if len(high_indices) >= 2:
        previous, current = high_indices[-2:]
        has_bearish = (
            recent_close.iloc[current] > recent_close.iloc[previous]
            and recent_rsi.iloc[current] < recent_rsi.iloc[previous]
        )

    has_bullish = False
    if len(low_indices) >= 2:
        previous, current = low_indices[-2:]
        has_bullish = (
            recent_close.iloc[current] < recent_close.iloc[previous]
            and recent_rsi.iloc[current] > recent_rsi.iloc[previous]
        )
    
    # 确定背离类型
    if has_bearish:
        divergence_type = "BEARISH"
    elif has_bullish:
        divergence_type = "BULLISH"
    else:
        divergence_type = "NONE"
    
    return DivergenceData(
        rsi_value=rsi_value,
        has_bullish_divergence=has_bullish,
        has_bearish_divergence=has_bearish,
        divergence_type=divergence_type
    )


def calculate_all_indicators(
    symbol: str,
    ohlcv_data: List[List]
) -> IndicatorSummary:
    """
    计算单个代码的所有指标。
    
    Args:
        symbol: 交易对代码
        ohlcv_data: OHLCV 数据 (通常为 1h 周期)
        
    Returns:
        IndicatorSummary 包含所有计算出的指标
    """
    df = create_dataframe(ohlcv_data)
    
    if len(df) < 200:
        raise InsufficientDataError(symbol, required=200, received=len(df))
    
    current_price = df['close'].iloc[-1]
    
    # 计算所有指标
    vwap = calculate_vwap(df)
    trend = calculate_emas(df)
    bollinger = calculate_bollinger_bands(df)
    atr, atr_percent = calculate_atr(df)
    rsi, rsi_condition = calculate_rsi(df)
    divergence = detect_divergence(df)
    sr_levels = detect_support_resistance(df)
    
    return IndicatorSummary(
        symbol=symbol,
        current_price=current_price,
        vwap=vwap,
        price_vs_vwap="ABOVE" if current_price > vwap else "BELOW",
        trend=trend,
        bollinger=bollinger,
        atr=atr,
        atr_percent=atr_percent,
        rsi=rsi,
        rsi_condition=rsi_condition,
        divergence=divergence,
        support_resistance=sr_levels
    )


def format_indicator_summary(summary: IndicatorSummary) -> str:
    """
    为 AI 上下文格式化指标摘要。
    
    Args:
        summary: IndicatorSummary 对象
        
    Returns:
        用于提示词的格式化字符串
    """
    values = {
        "ABOVE": "上方",
        "BELOW": "下方",
        "BULLISH": "看涨",
        "BEARISH": "看跌",
        "NEUTRAL": "中性",
        "STRONG": "强",
        "MODERATE": "中",
        "WEAK": "弱",
        "OVERBOUGHT": "超买",
        "OVERSOLD": "超卖",
        "NONE": "无",
    }
    return f"""[资产: {summary.symbol}]
- 价格: ${summary.current_price:,.2f} | VWAP: ${summary.vwap:,.2f} ({values.get(summary.price_vs_vwap, summary.price_vs_vwap)})
- 趋势: {values.get(summary.trend.trend_direction, summary.trend.trend_direction)} ({values.get(summary.trend.trend_strength, summary.trend.trend_strength)}) | EMA20: ${summary.trend.ema_20:,.2f}, EMA50: ${summary.trend.ema_50:,.2f}
- 结构: 支撑 ${summary.support_resistance.nearest_support:,.2f} | 阻力 ${summary.support_resistance.nearest_resistance:,.2f}
- 波动: ATR ${summary.atr:,.2f} ({summary.atr_percent:.2f}%) | 布林带 {'挤压' if summary.bollinger.is_squeeze else '正常'}
- RSI: {summary.rsi:.1f} ({values.get(summary.rsi_condition, summary.rsi_condition)}) | 背离: {values.get(summary.divergence.divergence_type, summary.divergence.divergence_type)}"""


def format_ohlcv_for_prompt(ohlcv: list, timeframe: str, limit: int = 100) -> str:
    """
    格式化 K 线数据供 AI 上下文使用。
    
    不同周期使用不同的格式：
    - 1m: 基础格式 (Close, Vol, MA5, MA60)
    - 15m: 含短周期指标 (RSI, BB%B, EMA20)
    - 1h/4h/1d: 含趋势指标 (RSI, MACD)
    
    Args:
        ohlcv: K 线数据列表 [[timestamp, open, high, low, close, volume], ...]
        timeframe: 时间周期标识 (1m, 15m, 1h, 4h, 1d)
        limit: 输出的 K 线数量 (默认 100，可通过配置覆盖)
        
    Returns:
        格式化的 K 线字符串
    """
    if not ohlcv or len(ohlcv) < 5:
        return f"[{timeframe} K线] 数据不足"

    normalized = create_dataframe(ohlcv).reset_index()
    ohlcv = [
        [
            int(row.timestamp.timestamp() * 1000),
            row.open,
            row.high,
            row.low,
            row.close,
            row.volume,
        ]
        for row in normalized.itertuples(index=False)
    ]
    
    # 确保不超过实际数据量
    actual_limit = min(limit, len(ohlcv))
    
    # 选择时间格式
    if timeframe == '1d':
        time_fmt = '%m/%d'
    elif timeframe in ('4h', '1h', '15m'):
        time_fmt = '%m/%d %H:%M'
    else:
        time_fmt = '%H:%M'
    
    # 15m 周期：短周期指标 (RSI, BB%B, EMA20)
    if timeframe == '15m' and len(ohlcv) >= 20:
        return _format_with_short_indicators(ohlcv, timeframe, actual_limit, time_fmt)
    
    # 1h/4h/1d 周期：趋势指标 (RSI, MACD)
    if timeframe in ('1h', '4h', '1d') and len(ohlcv) >= 30:
        return _format_with_trend_indicators(ohlcv, timeframe, actual_limit, time_fmt)
    
    # 1m 和数据不足的情况：基础格式
    return _format_basic(ohlcv, timeframe, actual_limit, time_fmt)


def _format_basic(ohlcv: list, timeframe: str, limit: int, time_fmt: str) -> str:
    """基础格式：Close, Vol, MA5, MA60"""
    closes = [c[4] for c in ohlcv]
    ma5 = calc_sma(closes, 5)
    ma60 = calc_sma(closes, 60) if len(closes) >= 60 else [None] * len(closes)
    
    lines = [f"[{timeframe} K线 (最近{limit}根)]"]
    lines.append("时间 | 收盘价 | 成交量 | MA5 | MA60")
    
    for i in range(-limit, 0):
        candle = ohlcv[i]
        ts = format_time(from_timestamp(candle[0], in_milliseconds=True), time_fmt)
        close = candle[4]
        volume = candle[5]
        
        ma5_val = ma5[i] if ma5[i] else close
        ma60_val = ma60[i] if ma60[i] else close
        
        lines.append(f"{ts} | ${close:,.2f} | {volume:,.0f} | ${ma5_val:,.2f} | ${ma60_val:,.2f}")
    
    return "\n".join(lines)


def _format_with_short_indicators(ohlcv: list, timeframe: str, limit: int, time_fmt: str) -> str:
    """
    15m 格式：附带短周期指标。
    
    指标包括：RSI(14), BB%B(20,2), EMA20
    """
    df = create_dataframe(ohlcv)
    
    # 计算指标序列
    rsi_series = _rsi(df['close'], 14)
    upper, middle, lower = _bollinger_bands(df['close'], 20, 2.0)
    ema20_series = _ema(df['close'], 20)
    
    # 计算 %B
    percent_b = (df['close'] - lower) / (upper - lower)
    percent_b = percent_b.fillna(0.5)
    
    lines = [f"[{timeframe} K线 (最近{limit}根) - 含指标]"]
    lines.append("时间 | 收盘价 | RSI | BB%B | EMA20 | 成交量")
    
    for i in range(-limit, 0):
        candle = ohlcv[i]
        ts = format_time(from_timestamp(candle[0], in_milliseconds=True), time_fmt)
        close = candle[4]
        volume = candle[5]
        
        rsi_val = rsi_series.iloc[i]
        bb_val = percent_b.iloc[i]
        ema20_val = ema20_series.iloc[i]
        
        # 格式化 RSI 状态标记
        rsi_str = f"{rsi_val:.0f}" if not pd.isna(rsi_val) else "不可用"
        if not pd.isna(rsi_val):
            if rsi_val >= 70:
                rsi_str += "↑"  # 超买
            elif rsi_val <= 30:
                rsi_str += "↓"  # 超卖
        
        # 格式化 %B
        if pd.isna(bb_val):
            bb_str = "不可用"
        elif bb_val > 1:
            bb_str = f"{bb_val:.2f}↑"
        elif bb_val < 0:
            bb_str = f"{bb_val:.2f}↓"
        else:
            bb_str = f"{bb_val:.2f}"
        
        ema20_str = f"${ema20_val:,.2f}" if not pd.isna(ema20_val) else "不可用"
        
        lines.append(f"{ts} | ${close:,.2f} | {rsi_str} | {bb_str} | {ema20_str} | {volume:,.0f}")
    
    return "\n".join(lines)


def _format_with_trend_indicators(ohlcv: list, timeframe: str, limit: int, time_fmt: str) -> str:
    """
    1h/4h/1d 格式：附带趋势指标。
    
    指标包括：RSI(14), MACD(12,26,9)
    用于确认更大周期的动量和背离情况。
    """
    df = create_dataframe(ohlcv)
    
    # 计算指标序列
    rsi_series = _rsi(df['close'], 14)
    macd_line, signal_line, histogram = _macd(df['close'], 12, 26, 9)
    
    lines = [f"[{timeframe} K线 (最近{limit}根) - 含趋势指标]"]
    lines.append("时间 | 收盘价 | RSI | MACD | 信号线 | 柱状图 | 成交量")
    
    for i in range(-limit, 0):
        candle = ohlcv[i]
        ts = format_time(from_timestamp(candle[0], in_milliseconds=True), time_fmt)
        close = candle[4]
        volume = candle[5]
        
        rsi_val = rsi_series.iloc[i]
        macd_val = macd_line.iloc[i]
        signal_val = signal_line.iloc[i]
        hist_val = histogram.iloc[i]
        
        # 格式化 RSI
        rsi_str = f"{rsi_val:.0f}" if not pd.isna(rsi_val) else "不可用"
        if not pd.isna(rsi_val):
            if rsi_val >= 70:
                rsi_str += "↑"
            elif rsi_val <= 30:
                rsi_str += "↓"
        
        # 格式化 MACD (根据价格缩放显示)
        if pd.isna(macd_val) or pd.isna(signal_val) or pd.isna(hist_val):
            macd_str = "不可用"
            signal_str = "不可用"
            hist_str = "不可用"
        else:
            macd_str = f"{macd_val:+.2f}"
            signal_str = f"{signal_val:+.2f}"
            # 柱状图带趋势标记 (第一根 K 线没有前一根对比)
            prev_hist = histogram.iloc[i-1] if i > -limit else hist_val
            if pd.isna(prev_hist):
                prev_hist = hist_val
            if hist_val > 0:
                hist_str = f"{hist_val:+.2f}▲" if hist_val > prev_hist else f"{hist_val:+.2f}"
            else:
                hist_str = f"{hist_val:+.2f}▼" if hist_val < prev_hist else f"{hist_val:+.2f}"
        
        lines.append(f"{ts} | ${close:,.2f} | {rsi_str} | {macd_str} | {signal_str} | {hist_str} | {volume:,.0f}")
    
    return "\n".join(lines)


# 保留旧函数名作为别名，保持向后兼容
def _format_15m_with_indicators(ohlcv: list, limit: int, time_fmt: str) -> str:
    """向后兼容的别名。"""
    return _format_with_short_indicators(ohlcv, '15m', limit, time_fmt)


