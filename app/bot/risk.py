"""配置驱动的交易动作风险校验。"""

import json
from decimal import Decimal, InvalidOperation
from typing import Iterable


class RiskValidationError(ValueError):
    """工具批次违反配置化风险规则。"""


def _decimal(value, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RiskValidationError(f"{field} 必须是有效数字") from exc
    if not result.is_finite():
        raise RiskValidationError(f"{field} 必须是有限值")
    return result


class RiskEngine:
    """在任何真实或模拟执行前验证完整工具批次。"""

    TARGETED_TOOLS = {
        "trade_in",
        "close_position",
        "set_leverage",
        "set_margin_mode",
        "modify_position",
        "cancel_orders",
        "cancel_order",
    }

    def __init__(self, config):
        self.config = config
        self.allowed_symbols = set(config.TRADING_SYMBOLS)

    def validate_batch(self, tool_calls: Iterable, broker, market_context=None) -> None:
        calls = list(tool_calls)
        if not calls:
            raise RiskValidationError("AI 未返回可执行工具")
        if len(calls) > self.config.AI_MAX_TOOL_CALLS:
            raise RiskValidationError(
                f"单次工具数量 {len(calls)} 超过配置上限 {self.config.AI_MAX_TOOL_CALLS}"
            )

        fingerprints = set()
        for call in calls:
            fingerprint = json.dumps(
                {"name": call.name, "args": call.args},
                sort_keys=True,
                ensure_ascii=False,
            )
            if fingerprint in fingerprints:
                raise RiskValidationError(f"批次包含重复工具调用: {call.name}")
            fingerprints.add(fingerprint)

        positions = broker.fetch_positions()
        total_notional = sum(
            abs(_decimal(position.get("notional", 0), "持仓名义价值"))
            for position in positions
        )
        position_notionals = {
            (position["symbol"], position["side"]): abs(
                _decimal(position.get("notional", 0), "持仓名义价值")
            )
            for position in positions
        }
        sides_by_symbol = {}
        for position in positions:
            sides_by_symbol.setdefault(position["symbol"], set()).add(position["side"])

        balance = broker.fetch_balance()
        free_balance = _decimal(balance.get("free", 0), "可用余额")

        # 杠杆为 None 表示交易所侧杠杆无法确认，不能当作 1 处理
        leverage_by_symbol = {}
        for position in positions:
            raw_leverage = position.get("leverage")
            leverage_by_symbol[position["symbol"]] = (
                None if raw_leverage is None else int(raw_leverage)
            )

        # 按批次顺序推演持仓，判断撤单后是否还有仓位失去保护
        remaining_sides = {
            symbol: set(sides) for symbol, sides in sides_by_symbol.items()
        }
        unprotected = {}

        for call in calls:
            args = call.args
            if call.name in self.TARGETED_TOOLS:
                target = args.get("target", "")
                if target not in self.allowed_symbols:
                    raise RiskValidationError(f"交易对不在允许列表中: {target}")

            if call.name == "set_leverage":
                leverage = int(args["leverage"])
                if leverage > self.config.RISK_MAX_LEVERAGE:
                    raise RiskValidationError(
                        f"杠杆 {leverage} 超过配置上限 {self.config.RISK_MAX_LEVERAGE}"
                    )
                leverage_by_symbol[args["target"]] = leverage

            elif call.name == "trade_in":
                if free_balance < self.config.RISK_MIN_FREE_BALANCE_USDT:
                    raise RiskValidationError(
                        "可用余额低于配置下限 "
                        f"{self.config.RISK_MIN_FREE_BALANCE_USDT} USDT"
                    )
                if market_context is not None and args["target"] not in market_context.assets:
                    raise RiskValidationError(
                        f"{args['target']} 缺少本周期核心行情，禁止开仓"
                    )
                amount = _decimal(args["count_usdt"], "count_usdt")
                if amount > self.config.RISK_MAX_SINGLE_TRADE_USDT:
                    raise RiskValidationError(
                        f"单笔金额 {amount} 超过配置上限 "
                        f"{self.config.RISK_MAX_SINGLE_TRADE_USDT} USDT"
                    )
                if self.config.RISK_REQUIRE_PROTECTIVE_ORDER and not (
                    args.get("stop_loss_price") or args.get("take_profit_price")
                ):
                    raise RiskValidationError("当前配置要求开仓必须提供保护单（止损或止盈）")
                key = (args["target"], args["side"])
                updated_position = position_notionals.get(key, Decimal("0")) + amount
                if updated_position > self.config.RISK_MAX_POSITION_NOTIONAL_USDT:
                    raise RiskValidationError(
                        f"{args['target']} {args['side']} 仓位将超过单仓上限"
                    )
                total_notional += amount
                if total_notional > self.config.RISK_MAX_TOTAL_NOTIONAL_USDT:
                    raise RiskValidationError("总名义价值将超过配置上限")
                position_notionals[key] = updated_position
                remaining_sides.setdefault(args["target"], set()).add(args["side"])
                if args["target"] in leverage_by_symbol:
                    leverage = leverage_by_symbol[args["target"]]
                    if leverage is None:
                        raise RiskValidationError(
                            f"无法确认 {args['target']} 的杠杆倍数，禁止开仓"
                        )
                else:
                    leverage = self.config.PAPER_DEFAULT_LEVERAGE
                if leverage > self.config.RISK_MAX_LEVERAGE:
                    raise RiskValidationError("当前交易对杠杆超过配置上限")

            elif call.name in {"close_position", "modify_position"}:
                target = args["target"]
                requested_side = args.get("side")
                available_sides = sides_by_symbol.get(target, set())
                if not available_sides:
                    raise RiskValidationError(f"不存在 {target} 的可用仓位")
                if requested_side and requested_side not in available_sides:
                    raise RiskValidationError(
                        f"不存在 {target} {requested_side} 仓位"
                    )
                if not requested_side and len(available_sides) > 1:
                    raise RiskValidationError(
                        f"{target} 同时存在双向仓位，必须提供 side"
                    )
                if call.name == "modify_position":
                    # 重建了保护单，该交易对不再处于失保状态
                    unprotected.pop(target, None)
                elif int(args["percentage"]) == 100:
                    # 全平后该方向已无仓位，无需再谈保护
                    sides = remaining_sides.get(target, set())
                    if requested_side:
                        sides.discard(requested_side)
                    else:
                        sides.clear()
                    if not sides:
                        remaining_sides.pop(target, None)
                        unprotected.pop(target, None)

            elif call.name == "cancel_orders":
                target = args["target"]
                # 只有撤掉全部挂单或专门撤止损，才会让仓位真正失去保护；
                # 单独撤止盈属于让利润奔跑的正常操作。
                removes_stop_loss = args.get("order_type", "all") in {
                    "all", "stop_loss"
                }
                if (
                    self.config.RISK_REQUIRE_PROTECTIVE_ORDER
                    and removes_stop_loss
                    and target in remaining_sides
                ):
                    unprotected[target] = call.name

            elif call.name == "update_memory":
                content = args.get("content", "")
                if len(content) > self.config.AI_MAX_MEMORY_CHARS:
                    raise RiskValidationError("记忆内容超过配置长度上限")

        if unprotected:
            details = "、".join(
                f"{symbol}({tool})" for symbol, tool in sorted(unprotected.items())
            )
            raise RiskValidationError(
                f"当前配置要求持仓必须有保护单，撤单后以下持仓将失去保护: {details}；"
                "请在同一批次内改用 modify_position 重建保护单，或先全部平仓"
            )
