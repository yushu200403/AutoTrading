"""根据订单证据修复异常决策，不重放未知的市价交易。"""

import json
import logging
import math

from ccxt import InsufficientFunds, InvalidOrder

from app import db
from app.models import TradeDecision, utc_now
from app.bot.executor import _child_order_id


logger = logging.getLogger(__name__)
RECONCILIATION_STATUSES = ("PENDING", "UNKNOWN", "PARTIAL", "CRITICAL")
MARKET_TOOLS = {"trade_in", "close_position", "set_leverage", "set_margin_mode"}


class RecoveryPending(RuntimeError):
    """本轮证据或修复条件不足，稍后继续。"""

    def __init__(self, message, blocked_tools=None):
        super().__init__(message)
        self.blocked_tools = blocked_tools or MARKET_TOOLS


def _number(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("订单数量或价格必须是非负有限数值")
    return number


def _terminal(order):
    return str(order.get("status", "")).lower() in {
        "closed", "filled", "canceled", "cancelled", "expired", "rejected",
    }


class DecisionRecovery:
    """每个周期推进恢复任务；单条记录失败不妨碍其余记录和模型调用。"""

    def __init__(self, engine):
        self.engine = engine
        self.broker = engine.broker
        self.executor = engine.executor

    @staticmethod
    def _save(decision, state):
        decision.recovery_state = json.dumps(state, ensure_ascii=False)
        db.session.commit()

    def run(self):
        report = {"resolved": [], "pending": []}
        decisions = TradeDecision.query.filter(
            TradeDecision.trading_mode == self.engine.trading_mode,
            TradeDecision.execution_status.in_(RECONCILIATION_STATUSES),
        ).order_by(TradeDecision.id).all()
        # 先复制标识，避免某条恢复回滚导致其余 ORM 对象失效。
        identities = [(d.id, d.symbol, d.tool_name) for d in decisions]
        for decision_id, symbol, tool_name in identities:
            try:
                decision = db.session.get(TradeDecision, decision_id)
                state = json.loads(decision.recovery_state or "{}")
                args = json.loads(decision.tool_args or "{}")
                self._recover(decision, args, state)
                state["resolved_at"] = utc_now().isoformat()
                state["previous_status"] = decision.execution_status
                decision.execution_status = "RECONCILED"
                self._save(decision, state)
                report["resolved"].append(decision_id)
            except Exception as exc:
                db.session.rollback()
                blocked = getattr(exc, "blocked_tools", MARKET_TOOLS | {"modify_position"})
                report["pending"].append({
                    "decision_id": decision_id, "symbol": symbol,
                    "tool": tool_name, "error": str(exc),
                    "blocked_tools": sorted(blocked),
                })
                logger.warning("决策 %s 自动修复尚未完成: %s", decision_id, exc)
        return report

    def _client_id(self, decision):
        if decision.client_order_id:
            return decision.client_order_id
        if not decision.cycle_id or not decision.tool_name:
            raise RecoveryPending("历史记录缺少周期或工具身份，无法确定原订单")
        index = TradeDecision.query.filter(
            TradeDecision.cycle_id == decision.cycle_id,
            TradeDecision.trading_mode == decision.trading_mode,
            TradeDecision.id < decision.id,
        ).count()
        return self.engine._client_order_id(decision.cycle_id, index, decision.tool_name)

    def _recover(self, decision, args, state):
        name = decision.tool_name
        symbol = decision.symbol
        if name == "update_memory":
            # 旧记忆可能已被后续周期覆盖，不用历史内容覆盖最新白板。
            state["note"] = "历史记忆意图已结束，由当前模型周期维护最新记忆"
            return
        if name in {"set_leverage", "set_margin_mode"}:
            newer = TradeDecision.query.filter(
                TradeDecision.trading_mode == decision.trading_mode,
                TradeDecision.symbol == symbol,
                TradeDecision.tool_name == name,
                TradeDecision.id > decision.id,
                TradeDecision.execution_status == "SUCCESS",
            ).first()
            if newer:
                state["note"] = f"账户设置已由后续成功决策 {newer.id} 更新"
                return
            positions = self.broker.fetch_positions()
            matching = [p for p in positions if p["symbol"] == symbol]
            field = "leverage" if name == "set_leverage" else "margin_mode"
            expected = str(args["leverage"] if name == "set_leverage" else args["mode"]).lower()
            if not matching or any(str(p.get(field, "")).lower() != expected for p in matching):
                raise RecoveryPending("尚不能从实际持仓确认账户设置，允许模型按最新状态重新设置", {"trade_in"})
            state["note"] = "账户设置与当前持仓一致"
            return
        if name in {"cancel_order", "cancel_orders"}:
            self._recover_cancellation(decision, args, state)
            return
        if name not in {"trade_in", "close_position", "modify_position"}:
            raise RecoveryPending("历史工具类型无法自动识别")

        base = self._client_id(decision)
        for step in state.get("steps", {}).values():
            if not self.broker.fetch_conditional_by_client_id(symbol, step["client_order_id"]):
                raise RecoveryPending("历史修复请求尚未确认，继续查询",
                                      {"trade_in", "modify_position"})
        for unknown in state.get("uncertain", []):
            lookup = (self.broker.fetch_conditional_by_client_id
                      if unknown["conditional"] else self.broker.fetch_order_by_client_id)
            order = lookup(symbol, unknown["client_order_id"])
            if not order or (not unknown["conditional"] and not _terminal(order)):
                raise RecoveryPending("关联订单结果仍不明确，继续按原订单 ID 查询",
                                      MARKET_TOOLS | {"modify_position"})

        if name != "modify_position":
            order = self.broker.fetch_order_by_client_id(symbol, base)
            if not order or not _terminal(order):
                raise RecoveryPending("市价订单尚未确认终态，禁止重复发送同方向交易")
            filled = _number(order.get("filled"))
            decision.order_id = str(order["id"])
            decision.executed_quantity = filled
            if order.get("average") is not None:
                decision.executed_price = _number(order["average"])
            state["market_order_confirmed"] = True
            state["position_side"] = (
                args.get("side") or order.get("positionSide")
                or (order.get("info") or {}).get("positionSide")
            )
            self._save(decision, state)
            if filled == 0:
                state["note"] = "已确认订单结束且没有成交"
                return

        requested_side = args.get("side") or state.get("position_side")
        position = self.broker.get_position_size(symbol, requested_side)
        side = position["side"] if position else requested_side
        if not side:
            raise RecoveryPending("无法确定需要修复的持仓方向")
        orders = self.executor._matching_protective_orders(symbol, side)
        if position is None:
            for order in orders:
                self._cancel(symbol, order)
            state["note"] = "该方向已无持仓，残留保护单已清理"
            return
        self._repair_protection(decision, args, state, position, orders)

    def _cancel(self, symbol, order):
        outcome = self.broker.cancel_order_by_id(symbol, str(order["id"]))
        if not outcome.get("success"):
            raise RecoveryPending(f"保护单 {order['id']} 撤销尚未确认")

    def _recover_cancellation(self, decision, args, state):
        orders = self.broker.get_open_orders(decision.symbol)
        if decision.tool_name == "cancel_order":
            remaining = [o for o in orders if str(o["id"]) == str(args["order_id"])]
        elif "cancel_targets" in state:
            targets = set(state["cancel_targets"])
            remaining = [o for o in orders if str(o["id"]) in targets]
        else:
            # 批量撤单意图缺少当时的订单清单时，不能误撤后来新建的保护单。
            marker = {"stop_loss": "STOP", "take_profit": "TAKE_PROFIT"}.get(args.get("order_type", "all"))
            remaining = [o for o in orders if marker is None or marker in str(o.get("type", "")).upper()]
            if remaining:
                raise RecoveryPending("旧批量撤单无法区分后续新订单，交由模型依据当前挂单处理", {"trade_in"})
        for order in remaining:
            self._cancel(decision.symbol, order)
        state["note"] = "目标订单已不在当前挂单中"

    def _repair_protection(self, decision, args, state, position, orders):
        symbol, side = position["symbol"], position["side"]
        quantity = _number(position["contracts"])
        if quantity <= 0:
            raise RecoveryPending("持仓数量无效")
        for marker, arg_name, suffix in (
            ("STOP", "stop_loss_price", "sl"),
            ("TAKE_PROFIT", "take_profit_price", "tp"),
        ):
            matching = [o for o in orders if marker in str(o.get("type", "")).upper()]
            adequate = [o for o in matching if math.isclose(_number(o.get("amount")), quantity, rel_tol=1e-8)]
            if adequate:
                # 当前已有效的保护优先于过时的历史触发价。
                keep = adequate[-1]
                for old in matching:
                    if str(old["id"]) != str(keep["id"]):
                        self._cancel(symbol, old)
                continue
            trigger = args.get(arg_name)
            if trigger is None and matching:
                trigger = matching[-1].get("stopPrice") or (matching[-1].get("info") or {}).get("triggerPrice")
            if trigger is None:
                has_profit_plan = args.get("take_profit_price") or any(
                    "TAKE_PROFIT" in str(order.get("type", "")).upper()
                    for order in orders
                )
                if (marker == "STOP" and self.engine.config.RISK_REQUIRE_PROTECTIVE_ORDER
                        and not has_profit_plan):
                    raise RecoveryPending("持仓缺少保护价，请模型按最新行情补充保护", {"trade_in"})
                continue
            trigger = _number(trigger)
            self._ensure_protection(decision, state, position, trigger, suffix)
            fresh = self.executor._matching_protective_orders(symbol, side)
            current = [o for o in fresh if marker in str(o.get("type", "")).upper()
                       and math.isclose(_number(o.get("amount")), quantity, rel_tol=1e-8)]
            if not current:
                raise RecoveryPending("修复订单尚未出现在当前挂单中", {"trade_in", "modify_position"})
            for old in matching:
                if str(old["id"]) != str(current[-1]["id"]):
                    self._cancel(symbol, old)
        state["note"] = "已按最新持仓核对并修复保护单"

    def _ensure_protection(self, decision, state, position, trigger, suffix):
        steps = state.setdefault("steps", {})
        step = steps.get(suffix)
        symbol, side = position["symbol"], position["side"]
        if step:
            order = self.broker.fetch_conditional_by_client_id(symbol, step["client_order_id"])
            if not order:
                raise RecoveryPending("修复请求已记录，结果尚未确认；继续查询，不重复创建",
                                      {"trade_in", "modify_position"})
            if _terminal(order):
                # 已触发或撤销的修复订单不能用原身份重建；当前模型可重新设价。
                raise RecoveryPending("历史修复订单已结束，请模型按最新持仓重新设置保护", {"trade_in"})
            return
        try:
            self.executor._validate_protective_prices(
                symbol, side, trigger if suffix == "sl" else None,
                trigger if suffix == "tp" else None,
            )
        except ValueError as exc:
            raise RecoveryPending(f"历史保护价已不适用，请模型重新设价: {exc}", {"trade_in"}) from exc
        client_id = _child_order_id(self._client_id(decision), f"repair-{suffix}")
        steps[suffix] = {"client_order_id": client_id, "submitted_at": utc_now().isoformat()}
        # 先落盘再发送；即使写入成功后进程中断，也不会盲目重发。
        self._save(decision, state)
        create = (self.broker.create_stop_loss_order if suffix == "sl"
                  else self.broker.create_take_profit_order)
        try:
            order = create(symbol, "SELL" if side == "LONG" else "BUY",
                           position["contracts"], trigger, side, client_order_id=client_id)
        except (InvalidOrder, InsufficientFunds, ValueError) as exc:
            # 明确拒绝不属于未知成交，下一周期可以按新状态再次修复。
            steps.pop(suffix)
            state["last_repair_error"] = str(exc)
            self._save(decision, state)
            raise RecoveryPending(f"保护单修复被拒绝，请模型调整: {exc}", {"trade_in"}) from exc
        steps[suffix]["order_id"] = str(order["id"])
        self._save(decision, state)
