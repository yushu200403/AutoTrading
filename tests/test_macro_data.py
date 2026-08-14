"""宏观市场摘要回归测试。"""

import pytest

from app.bot.macro_data import MacroDataClient


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (None, "不可用"),
        (float("nan"), "不可用"),
        (float("inf"), "不可用"),
        (float("-inf"), "不可用"),
        (2.0, "强劲"),
        (1.2, "健康"),
        (0.8, "疲软"),
        (0.3, "非常疲软"),
        (9999, "9999+"),
    ],
)
def test_macro_summary_levels(ratio, expected):
    client = MacroDataClient()
    assert expected in client.format_macro_summary(ratio)
