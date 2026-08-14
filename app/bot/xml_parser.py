"""
AI 响应的 XML 工具调用解析器。

从 AI 生成的文本中提取并验证 <tooluse> 块。
遵循规范中定义的 XML-MCP 协议。
"""

import re
import json
import logging
from decimal import Decimal, InvalidOperation
from typing import List, Dict, Any
from dataclasses import dataclass

from app.bot.prompts import TOOL_DEFINITIONS, get_tool_names, LEVERAGE_MIN, LEVERAGE_MAX

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    """表示从 AI 响应中解析出的工具调用。"""
    name: str
    info: str
    args: Dict[str, Any]
    raw_json: str  # 模型原始输出，用于审计追溯，不含规范化结果
    
    def __repr__(self):
        info_preview = (self.info[:50] + '...') if len(self.info) > 50 else self.info
        return f"<ToolCall {self.name}: {info_preview}>"


class XMLParseError(Exception):
    """当 XML 解析失败时引发。"""
    
    def __init__(self, message: str, raw_content: str = None):
        self.raw_content = raw_content
        super().__init__(message)


class ToolValidationError(Exception):
    """当工具验证失败时引发。"""
    
    def __init__(self, tool_name: str, reason: str):
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(f"工具 {tool_name} 校验失败：{reason}")


# 提取 <tooluse>...</tooluse> 块的正则表达式
TOOLUSE_PATTERN = re.compile(
    r'<tooluse>\s*(.*?)\s*</tooluse>',
    re.DOTALL | re.IGNORECASE
)


def extract_tooluse_blocks(text: str) -> List[str]:
    """
    从文本中提取所有 <tooluse> 块。
    
    参数:
        text: AI 响应文本
        
    返回:
        tooluse 标签内的 JSON 字符串列表
    """
    matches = TOOLUSE_PATTERN.findall(text)
    return [m.strip() for m in matches]


def parse_json_safely(json_str: str) -> Dict[str, Any]:
    """
    严格解析 JSON 字符串，不猜测或修复模型意图。
    
    参数:
        json_str: 要解析的 JSON 字符串
        
    返回:
        解析后的字典
        
    异常:
        XMLParseError: 如果 JSON 无法解析
    """
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise XMLParseError(
            f"JSON 解析失败：{e}",
            raw_content=json_str
        ) from e


def _validate_leverage(args: Dict[str, Any], arg_name: str, tool_name: str) -> None:
    """
    验证杠杆参数并规范化为字符串。
    
    参数:
        args: 工具参数字典 (会被原地修改)
        arg_name: 杠杆参数名 ('leverage')
        tool_name: 工具名称 (用于错误消息)
        
    异常:
        ToolValidationError: 如果杠杆无效
    """
    if arg_name not in args:
        return
    
    try:
        value = Decimal(str(args[arg_name]))
        if not value.is_finite() or value != value.to_integral_value():
            raise ValueError()
        lev = int(value)
        if not LEVERAGE_MIN <= lev <= LEVERAGE_MAX:
            raise ValueError()
        args[arg_name] = str(lev)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ToolValidationError(
            tool_name,
            f"{arg_name} 必须在 {LEVERAGE_MIN} 到 {LEVERAGE_MAX} 之间，"
            f"实际为 {args.get(arg_name)}"
        ) from exc


def _validate_positive_number(args: Dict[str, Any], arg_name: str, tool_name: str) -> None:
    """
    验证数字参数为正数。
    
    参数:
        args: 工具参数字典
        arg_name: 参数名
        tool_name: 工具名称
        
    异常:
        ToolValidationError: 如果不是正数
    """
    if arg_name not in args:
        return
    
    try:
        value = Decimal(str(args[arg_name]))
        if not value.is_finite() or value <= 0:
            raise ValueError()
        args[arg_name] = str(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ToolValidationError(
            tool_name,
            f"{arg_name} 必须是正数，实际为 {args.get(arg_name)}"
        ) from exc


def validate_tool_call(
    tool_data: Dict[str, Any], raw_json: str = None
) -> ToolCall:
    """
    验证并从解析数据创建 ToolCall。
    
    参数:
        tool_data: 从 JSON 解析出的字典
        raw_json: 模型原始输出片段，用于审计追溯
        
    返回:
        验证后的 ToolCall 对象
        
    异常:
        ToolValidationError: 如果验证失败
    """
    if not isinstance(tool_data, dict):
        raise ToolValidationError("unknown", "工具调用必须是 JSON 对象")
    unknown_fields = set(tool_data) - {"name", "info", "args"}
    if unknown_fields:
        raise ToolValidationError("unknown", f"未知顶层字段: {sorted(unknown_fields)}")

    # 检查必需字段
    if 'name' not in tool_data:
        raise ToolValidationError("未知", "缺少 name 字段")
    
    name = tool_data['name']
    if not isinstance(name, str):
        raise ToolValidationError("unknown", "name 必须是字符串")
    
    if name not in get_tool_names():
        raise ToolValidationError(name, f"未知工具，可用工具为 {get_tool_names()}")
    
    if 'args' not in tool_data:
        raise ToolValidationError(name, "缺少 args 字段")
    
    # 获取工具定义
    tool_def = TOOL_DEFINITIONS[name]
    args = tool_data['args']
    
    # 验证 args 是字典类型
    if not isinstance(args, dict):
        raise ToolValidationError(name, f"args 必须是对象，实际为 {type(args).__name__}")

    allowed_args = set(tool_def['required_args']) | set(tool_def['optional_args'])
    unknown_args = set(args) - allowed_args
    if unknown_args:
        raise ToolValidationError(name, f"未知参数: {sorted(unknown_args)}")
    
    # 检查必需参数
    for required_arg in tool_def['required_args']:
        if required_arg not in args or args[required_arg] in (None, ''):
            raise ToolValidationError(
                name, 
                f"缺少必填参数: {required_arg}"
            )

    for text_arg in (
        "target",
        "side",
        "reason",
        "content",
        "order_id",
        "mode",
        "order_type",
    ):
        if text_arg in args and not isinstance(args[text_arg], str):
            raise ToolValidationError(name, f"{text_arg} 必须是字符串")
    
    # 验证特定工具参数
    if name == "trade_in":
        side = args.get('side', '').upper()
        if side not in tool_def['side_values']:
            raise ToolValidationError(
                name,
                f"方向 {side} 无效，必须是 LONG 或 SHORT"
            )
        # 将方向规范化为大写
        args['side'] = side
        
        # 验证 count_usdt 是正数
        _validate_positive_number(args, 'count_usdt', name)
        
        # 验证止损止盈价格是正数
        _validate_positive_number(args, 'stop_loss_price', name)
        _validate_positive_number(args, 'take_profit_price', name)
    
    if name == "close_position":
        try:
            value = Decimal(str(args['percentage']))
            if not value.is_finite() or value != value.to_integral_value():
                raise ValueError()
            pct = int(value)
            if not 1 <= pct <= 100:
                raise ValueError()
            # 为了一致性规范化为字符串
            args['percentage'] = str(pct)
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ToolValidationError(
                name,
                f"percentage 必须是 1 到 100 的整数，实际为 {args.get('percentage')}"
            ) from exc
        _normalize_optional_side(args, name)
    
    if name == "set_leverage":
        _validate_leverage(args, 'leverage', name)
    
    if name == "set_margin_mode":
        mode = args.get('mode', '').lower()
        if mode not in ['cross', 'isolated']:
            raise ToolValidationError(
                name,
                f"保证金模式 {mode} 无效，必须是 cross 或 isolated"
            )
        args['mode'] = mode
    
    if name == "modify_position":
        # 至少需要提供 stop_loss_price 或 take_profit_price 之一
        if 'stop_loss_price' not in args and 'take_profit_price' not in args:
            raise ToolValidationError(
                name,
                "必须至少提供 stop_loss_price 或 take_profit_price"
            )
        # 验证价格是正数
        _validate_positive_number(args, 'stop_loss_price', name)
        _validate_positive_number(args, 'take_profit_price', name)
        _normalize_optional_side(args, name)
    
    if name == "cancel_orders":
        # 验证 order_type (如果提供)
        order_type = args.get('order_type', 'all').lower()
        if order_type not in ['stop_loss', 'take_profit', 'all']:
            raise ToolValidationError(
                name,
                f"订单类型 {order_type} 无效，必须是 stop_loss、take_profit 或 all"
            )
        args['order_type'] = order_type
    
    if name == "cancel_order":
        # 验证 order_id 必须提供
        if 'order_id' not in args or not args['order_id']:
            raise ToolValidationError(
                name,
                "cancel_order 必须提供 order_id"
            )
    if "target" in args and len(args["target"]) > 20:
        raise ToolValidationError(name, "target 长度不能超过 20")
    info = tool_data.get('info', '')
    if not isinstance(info, str):
        raise ToolValidationError(name, "info 必须是字符串")
    if len(info) > 80:
        raise ToolValidationError(name, "info 长度不能超过 80")

    # 创建工具调用对象；args 已被原地规范化，
    # 因此 raw_json 优先保留模型原始文本以保证审计可追溯
    return ToolCall(
        name=name,
        info=info,
        args=args,
        raw_json=(
            raw_json
            if raw_json is not None
            else json.dumps(tool_data, ensure_ascii=False)
        )
    )


def _normalize_optional_side(args: Dict[str, Any], tool_name: str) -> None:
    if "side" not in args:
        return
    side = str(args["side"]).upper()
    if side not in {"LONG", "SHORT"}:
        raise ToolValidationError(tool_name, "side 必须是 LONG 或 SHORT")
    args["side"] = side


def parse_tool_calls(response_text: str, strict: bool = True) -> List[ToolCall]:
    """
    从 AI 响应文本中解析所有工具调用。
    
    这是 XML 解析的主要入口点。
    
    参数:
        response_text: 完整的 AI 响应文本
        
    返回:
        验证后的工具调用对象列表
    """
    tool_calls = []
    
    # 提取所有 tooluse 块
    json_blocks = extract_tooluse_blocks(response_text)
    
    if not json_blocks:
        logger.warning("AI 响应中没有 tooluse 块")
        return []
    
    errors = []
    # 解析并验证每个块
    for i, json_str in enumerate(json_blocks):
        try:
            tool_data = parse_json_safely(json_str)
            tool_call = validate_tool_call(tool_data, raw_json=json_str)
            tool_calls.append(tool_call)
            
            logger.debug("工具调用解析成功: %s", tool_call.name)
            
        except (XMLParseError, ToolValidationError) as e:
            logger.error("第 %d 个工具调用解析失败: %s", i + 1, e)
            errors.append(f"第 {i + 1} 个工具调用无效: {e}")

    if strict and errors:
        raise XMLParseError("；".join(errors), raw_content=response_text)
    
    return tool_calls


def has_memory_update(tool_calls: List[ToolCall]) -> bool:
    """检查工具调用是否包含记忆更新。"""
    return any(tc.name == "update_memory" for tc in tool_calls)


def get_trading_actions(tool_calls: List[ToolCall]) -> List[ToolCall]:
    """获取仅与交易相关的工具调用。"""
    return [tc for tc in tool_calls if tc.name in ("trade_in", "close_position")]


def format_tool_calls_summary(tool_calls: List[ToolCall]) -> str:
    """
    将工具调用格式化为人类可读的摘要。
    
    参数:
        tool_calls: 解析后的工具调用列表
        
    返回:
        格式化的摘要字符串
    """
    if not tool_calls:
        return "未执行任何动作。"
    
    lines = []
    for tc in tool_calls:
        if tc.name == "trade_in":
            lines.append(f"[交易] {tc.args.get('side')} {tc.args.get('target')}：{tc.info}")
        elif tc.name == "close_position":
            lines.append(f"[平仓] {tc.args.get('target')} {tc.args.get('percentage')}%：{tc.info}")
        elif tc.name == "set_leverage":
            lines.append(f"[杠杆] {tc.args.get('target')} -> {tc.args.get('leverage')}x")
        elif tc.name == "set_margin_mode":
            lines.append(f"[保证金] {tc.args.get('target')} -> {tc.args.get('mode')}")
        elif tc.name == "modify_position":
            lines.append(f"[修改] {tc.args.get('target')}：{tc.info}")
        elif tc.name == "cancel_orders":
            lines.append(f"[撤单] {tc.args.get('target')} ({tc.args.get('order_type', 'all')})")
        elif tc.name == "cancel_order":
            lines.append(f"[按编号撤单] {tc.args.get('target')} order_id={tc.args.get('order_id')}")
        elif tc.name == "update_memory":
            content = tc.args.get('content', '')
            preview = (content[:50] + '...') if len(content) > 50 else content
            lines.append(f"[记忆] {preview}")
    
    return "\n".join(lines)
