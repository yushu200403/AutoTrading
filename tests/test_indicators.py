"""技术指标回归测试。"""

import pytest

from app.bot.indicators import (
    calculate_all_indicators,
    calculate_rsi,
    create_dataframe,
)


def _candles(count=250, close=100.0):
    return [
        [
            1_700_000_000_000 + index * 3_600_000,
            close,
            close + 1,
            close - 1,
            close,
            1000 + index,
        ]
        for index in range(count)
    ]


def test_flat_market_rsi_is_neutral_after_warmup():
    dataframe = create_dataframe(_candles())
    value, condition = calculate_rsi(dataframe)
    assert value == 50
    assert condition == "NEUTRAL"


def test_indicator_summary_uses_real_200_plus_candles():
    candles = _candles()
    for index, candle in enumerate(candles):
        candle[1] += index * 0.1
        candle[2] += index * 0.1
        candle[3] += index * 0.1
        candle[4] += index * 0.1
    summary = calculate_all_indicators("BTC/USDT", candles)
    assert summary.current_price == candles[-1][4]
    assert summary.trend.ema_200 != summary.current_price


def test_invalid_ohlc_relationship_is_rejected():
    candles = _candles(20)
    candles[-1][2] = 50
    try:
        create_dataframe(candles)
    except ValueError as exc:
        assert "高低价关系" in str(exc)
    else:
        raise AssertionError("非法 OHLCV 未被拒绝")


def test_dataframe_sorts_and_deduplicates_timestamps():
    candles = _candles(3)
    earlier, duplicate, later = candles
    duplicate = duplicate.copy()
    duplicate[0] = earlier[0]
    duplicate[1:5] = [120, 121, 119, 120]

    dataframe = create_dataframe([later, earlier, duplicate])

    assert dataframe.index.is_monotonic_increasing
    assert len(dataframe) == 2
    assert dataframe.iloc[0]["close"] == 120
    assert dataframe.iloc[-1]["close"] == later[4]


@pytest.mark.parametrize("timestamp", [-1, float("nan"), float("inf")])
def test_invalid_timestamps_are_rejected(timestamp):
    candles = _candles(5)
    candles[-1][0] = timestamp

    with pytest.raises(ValueError, match="时间戳|非有限"):
        create_dataframe(candles)


def test_rsi_uses_wilder_recursive_smoothing():
    candles = _candles(4)
    for candle, close in zip(candles, [1, 2, 1, 2], strict=True):
        candle[1:5] = [close, close, close, close]
    value, condition = calculate_rsi(create_dataframe(candles), length=3)

    assert value == pytest.approx(68.4210526)
    assert condition == "NEUTRAL"
