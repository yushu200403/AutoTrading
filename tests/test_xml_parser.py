"""AI 工具协议回归测试。"""

import pytest

from app.bot.xml_parser import XMLParseError, parse_tool_calls


def _block(payload):
    import json
    return f"<tooluse>{json.dumps(payload)}</tooluse>"


def test_strict_mode_rejects_entire_batch_when_one_call_is_invalid():
    response = _block({
        "name": "update_memory",
        "info": "更新",
        "args": {"content": "等待"},
    }) + _block({
        "name": "trade_in",
        "info": "非法金额",
        "args": {"target": "BTC/USDT", "side": "LONG", "count_usdt": "NaN"},
    })
    with pytest.raises(XMLParseError):
        parse_tool_calls(response, strict=True)


def test_strict_json_rejects_trailing_comma():
    response = (
        '<tooluse>{"name":"update_memory","info":"更新",'
        '"args":{"content":"等待",}}</tooluse>'
    )
    with pytest.raises(XMLParseError, match="JSON 解析失败"):
        parse_tool_calls(response, strict=True)


def test_close_position_normalizes_explicit_side():
    calls = parse_tool_calls(_block({
        "name": "close_position",
        "info": "减仓",
        "args": {
            "target": "BTC/USDT",
            "side": "long",
            "percentage": "25",
            "reason": "降低风险",
        },
    }))
    assert calls[0].args["side"] == "LONG"
    assert calls[0].args["percentage"] == "25"


def test_fractional_percentage_and_unknown_args_are_rejected():
    with pytest.raises(XMLParseError):
        parse_tool_calls(_block({
            "name": "close_position",
            "info": "减仓",
            "args": {
                "target": "BTC/USDT",
                "percentage": "25.5",
                "reason": "测试",
                "unexpected": True,
            },
        }))


@pytest.mark.parametrize(
    "payload",
    [
        {
            "name": "trade_in",
            "info": "非法方向",
            "args": {
                "target": "BTC/USDT",
                "side": 1,
                "count_usdt": "100",
                "stop_loss_price": "90",
            },
        },
        {
            "name": "set_margin_mode",
            "info": "非法模式",
            "args": {"target": "BTC/USDT", "mode": 1},
        },
        {
            "name": "cancel_orders",
            "info": "非法订单类型",
            "args": {"target": "BTC/USDT", "order_type": 1},
        },
    ],
)
def test_text_arguments_reject_non_string_values(payload):
    with pytest.raises(XMLParseError, match="字符串"):
        parse_tool_calls(_block(payload))


def test_raw_json_preserves_model_output_for_audit():
    """args 会被原地规范化，raw_json 必须保留模型原始文本。"""
    raw = (
        '<tooluse>{"name":"close_position","info":"减仓",'
        '"args":{"target":"BTC/USDT","side":"long",'
        '"percentage":25,"reason":"降低风险"}}</tooluse>'
    )
    call = parse_tool_calls(raw)[0]
    assert '"side":"long"' in call.raw_json
    assert '"percentage":25' in call.raw_json
    # 规范化结果只体现在 args 上
    assert call.args["side"] == "LONG"
    assert call.args["percentage"] == "25"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"info": "缺少名称", "args": {}}, "缺少 name"),
        (
            {"name": "不存在的工具", "info": "未知", "args": {}},
            "未知工具",
        ),
        (
            {"name": "update_memory", "info": "缺少参数", "args": {}},
            "缺少必填参数",
        ),
        (
            {"name": "update_memory", "info": "参数非对象", "args": "文本"},
            "args 必须是对象",
        ),
        (
            {
                "name": "update_memory",
                "info": "多余字段",
                "args": {"content": "x"},
                "extra": 1,
            },
            "未知顶层字段",
        ),
        (
            {
                "name": "modify_position",
                "info": "缺少价格",
                "args": {"target": "BTC/USDT"},
            },
            "必须至少提供",
        ),
        (
            {
                "name": "set_leverage",
                "info": "超范围杠杆",
                "args": {"target": "BTC/USDT", "leverage": "200"},
            },
            "leverage 必须在",
        ),
        (
            {
                "name": "trade_in",
                "info": "负数金额",
                "args": {
                    "target": "BTC/USDT",
                    "side": "LONG",
                    "count_usdt": "-100",
                    "stop_loss_price": "90",
                },
            },
            "必须是正数",
        ),
        (
            {
                "name": "update_memory",
                "info": "超长目标" * 21,
                "args": {"content": "x"},
            },
            "info 长度",
        ),
    ],
)
def test_protocol_violations_are_rejected(payload, message):
    with pytest.raises(XMLParseError, match=message):
        parse_tool_calls(_block(payload))


def test_missing_tooluse_block_returns_empty_list():
    assert parse_tool_calls("只有分析文本，没有工具调用") == []
