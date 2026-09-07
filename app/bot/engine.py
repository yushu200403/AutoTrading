"""交易周期协调器。"""

import json
import logging
from threading import Lock
from typing import Optional
from uuid import uuid4

from app import db
from app.models import (
    EquitySnapshot,
    MarketSnapshot,
    MemoryBoard,
    SystemSettings,
    TradeDecision,
    TradingCycle,
    utc_now,
)
from app.bot.ai_agent import AIAgent, AIAgentError, AIResponse
from app.bot.data_engine import DataEngine, MarketContext
from app.bot.executor import ExecutionResult, TradeExecutor
from app.bot.recovery import DecisionRecovery, MARKET_TOOLS, RECONCILIATION_STATUSES
from app.bot.paper_broker import PaperBroker
from app.bot.prompts import build_system_prompt, build_user_prompt
from app.bot.risk import RiskEngine, RiskValidationError
from app.bot.tz_utils import now_with_tz
from app.bot.xml_parser import ToolCall
from config import get_config


logger = logging.getLogger(__name__)


class TradingEngine:
    """协调真实行情、模型决策、风控与交易执行。"""

    # 这些执行状态意味着本地记录与交易执行端可能不一致：
    # PENDING 写入意图后未回写，UNKNOWN 请求结果不明，
    # PARTIAL 已成交但保护单未同步，CRITICAL 补偿动作本身失败。
    # 后续周期自动推进对账，模型持续分析；仅隔离可能重复执行的动作。
    RECONCILIATION_STATUSES = RECONCILIATION_STATUSES

    TOOL_ACTION_MAP = {
        "trade_in": lambda args: (args.get("side", "LONG"), args.get("target", "UNKNOWN")),
        "close_position": lambda args: ("CLOSE", args.get("target", "UNKNOWN")),
        "update_memory": lambda args: ("MEMORY", "SYSTEM"),
        "set_leverage": lambda args: ("LEVERAGE", args.get("target", "UNKNOWN")),
        "set_margin_mode": lambda args: ("MARGIN", args.get("target", "UNKNOWN")),
        "modify_position": lambda args: ("MODIFY", args.get("target", "UNKNOWN")),
        "cancel_orders": lambda args: ("CANCEL", args.get("target", "UNKNOWN")),
        "cancel_order": lambda args: ("CANCEL_ID", args.get("target", "UNKNOWN")),
    }

    def __init__(
        self,
        binance_api_key: str = "",
        binance_api_secret: str = "",
        ai_api_key: str = "",
        live_trading: Optional[bool] = None,
    ):
        if live_trading is not None and not isinstance(live_trading, bool):
            raise TypeError("交易模式开关必须是布尔值或空值")
        self.config = get_config()
        self.data_engine = DataEngine(binance_api_key, binance_api_secret)
        self.ai_agent = AIAgent(api_key=ai_api_key)
        self.paper_broker = PaperBroker(self.data_engine.binance, self.config)
        configured_live = self.config.TRADING_MODE == "live"
        self._executors = {
            "paper": TradeExecutor(self.paper_broker, self.config),
            "live": TradeExecutor(self.data_engine.binance, self.config),
        }
        self._trading_mode = "paper"
        self.executor = self._executors["paper"]
        self.risk_engine = RiskEngine(self.config)
        self._cycle_lock = Lock()
        requested_live = configured_live if live_trading is None else live_trading
        if requested_live:
            self.enable_live_trading(True)

    @property
    def trading_mode(self) -> str:
        return self._trading_mode

    @property
    def live_trading(self) -> bool:
        return self._trading_mode == "live"

    @property
    def broker(self):
        return self.data_engine.binance if self.live_trading else self.paper_broker

    def enable_live_trading(self, enable: bool = True):
        if not isinstance(enable, bool):
            raise TypeError("交易模式开关必须是布尔值")
        if enable:
            if self.config.LIVE_TRADING_CONFIRMATION != "I_UNDERSTAND_REAL_ORDERS":
                raise RuntimeError("未通过环境变量确认实盘交易")
            if not self.config.BINANCE_API_KEY or not self.config.BINANCE_API_SECRET:
                raise RuntimeError("实盘交易缺少币安 API 凭证")
        self._trading_mode = "live" if enable else "paper"
        self.executor = self._executors[self._trading_mode]
        logger.warning("交易模式已切换为 %s", self._trading_mode)

    def set_custom_instructions(self, instructions: str):
        if len(instructions) > self.config.CUSTOM_INSTRUCTIONS_MAX_CHARS:
            raise ValueError("自定义指令超过配置长度上限")
        settings = SystemSettings.get_or_create()
        settings.update_instructions(instructions)

    def _get_custom_instructions(self) -> str:
        try:
            return SystemSettings.get_or_create().custom_instructions or ""
        except Exception:
            db.session.rollback()
            raise

    def _get_memory_content(self) -> str:
        try:
            return MemoryBoard.get_or_create().content
        except Exception:
            db.session.rollback()
            raise

    def _save_memory_content(self, content: str) -> bool:
        if len(content) > self.config.AI_MAX_MEMORY_CHARS:
            raise ValueError("记忆内容超过配置长度上限")
        try:
            MemoryBoard.get_or_create().update(content)
            return True
        except Exception:
            db.session.rollback()
            raise

    def _save_snapshot(self, context: MarketContext) -> MarketSnapshot:
        try:
            snapshot = MarketSnapshot(
                timestamp=context.timestamp,
                advance_decline_ratio=context.advance_decline_ratio,
                indicators_data=json.dumps(
                    self.data_engine.to_dict(context), ensure_ascii=False
                ),
            )
            db.session.add(snapshot)
            db.session.commit()
            return snapshot
        except Exception:
            db.session.rollback()
            raise

    def _save_equity_snapshot(self) -> None:
        try:
            balance = self.broker.fetch_balance()
            positions = self.broker.fetch_positions()
            unrealized = sum(
                float(position.get("unrealized_pnl") or 0) for position in positions
            )
            snapshot = EquitySnapshot(
                trading_mode=self.trading_mode,
                total_equity=balance.get("total", 0),
                free_balance=balance.get("free", 0),
                unrealized_pnl=unrealized,
                position_count=len(positions),
            )
            db.session.add(snapshot)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

    def _save_decision_intent(
        self,
        cycle_id: str,
        tool_call: ToolCall,
        ai_reasoning: str,
        snapshot: MarketSnapshot,
        client_order_id: Optional[str] = None,
    ) -> TradeDecision:
        mapper = self.TOOL_ACTION_MAP.get(tool_call.name)
        action, symbol = mapper(tool_call.args) if mapper else (
            tool_call.name.upper(),
            "UNKNOWN",
        )
        decision = TradeDecision(
            timestamp=utc_now(),
            cycle_id=cycle_id,
            trading_mode=self.trading_mode,
            symbol=symbol,
            action=action,
            display_info=tool_call.info,
            tool_name=tool_call.name,
            tool_args=json.dumps(tool_call.args, ensure_ascii=False),
            ai_reasoning=ai_reasoning,
            snapshot_id=snapshot.id,
            execution_status="PENDING",
            client_order_id=client_order_id,
        )
        if tool_call.name == "cancel_orders":
            marker = {"stop_loss": "STOP", "take_profit": "TAKE_PROFIT"}.get(
                tool_call.args.get("order_type", "all")
            )
            orders = self.broker.get_open_orders(symbol)
            decision.recovery_state = json.dumps({"cancel_targets": [
                str(order["id"]) for order in orders
                if marker is None or marker in str(order.get("type", "")).upper()
            ]}, ensure_ascii=False)
        try:
            db.session.add(decision)
            db.session.commit()
            return decision
        except Exception:
            db.session.rollback()
            raise

    @staticmethod
    def _finalize_decision(
        decision: TradeDecision,
        execution_result: Optional[ExecutionResult],
        status_override: Optional[str] = None,
    ) -> None:
        decision.execution_status = status_override or (
            execution_result.status if execution_result else "SUCCESS"
        )
        if execution_result:
            decision.order_id = execution_result.order_id
            decision.executed_price = execution_result.executed_price
            decision.executed_quantity = execution_result.quantity
            decision.execution_error = execution_result.error
            if execution_result.recovery:
                state = json.loads(decision.recovery_state or "{}")
                state.update(execution_result.recovery)
                decision.recovery_state = json.dumps(state, ensure_ascii=False)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

    @staticmethod
    def _client_order_id(cycle_id: str, index: int, name: str) -> str:
        compact_cycle = cycle_id.replace("-", "")[:20]
        return f"onf-{compact_cycle}-{index:02d}-{name[:5]}"[:36]

    def _execute_tool(
        self, tool_call: ToolCall, client_order_id: str
    ) -> tuple[bool, Optional[ExecutionResult]]:
        args = tool_call.args
        if tool_call.name == "update_memory":
            return self._save_memory_content(args["content"]), None
        if tool_call.name == "trade_in":
            result = self.executor.open_position(
                symbol=args["target"],
                side=args["side"],
                amount_usdt=float(args["count_usdt"]),
                stop_loss_price=(
                    float(args["stop_loss_price"])
                    if args.get("stop_loss_price") else None
                ),
                take_profit_price=(
                    float(args["take_profit_price"])
                    if args.get("take_profit_price") else None
                ),
                client_order_id=client_order_id,
            )
        elif tool_call.name == "close_position":
            result = self.executor.close_position(
                symbol=args["target"],
                percentage=int(args["percentage"]),
                reason=args["reason"],
                position_side=args.get("side"),
                client_order_id=client_order_id,
            )
        elif tool_call.name == "set_leverage":
            result = self.executor.set_leverage(args["target"], int(args["leverage"]))
        elif tool_call.name == "set_margin_mode":
            result = self.executor.set_margin_mode(args["target"], args["mode"])
        elif tool_call.name == "modify_position":
            result = self.executor.modify_position_tpsl(
                args["target"],
                float(args["stop_loss_price"])
                    if args.get("stop_loss_price") else None,
                float(args["take_profit_price"])
                    if args.get("take_profit_price") else None,
                position_side=args.get("side"),
                client_order_id=client_order_id,
            )
        elif tool_call.name == "cancel_orders":
            result = self.executor.cancel_orders(
                args["target"], args.get("order_type", "all")
            )
        elif tool_call.name == "cancel_order":
            result = self.executor.cancel_order_by_id(
                args["target"], args["order_id"]
            )
        else:
            return False, ExecutionResult(
                False, status="FAILED", error=f"未知工具: {tool_call.name}"
            )
        return result.success, result

    def _unprotected_positions(self) -> list:
        """列出当前缺少止损保护的持仓。

        风控无法在批次阶段判断按 ID 撤单会撤掉哪类挂单，
        因此在周期末统一核对一次，确保失保状态对用户可见。
        """
        if not self.config.RISK_REQUIRE_PROTECTIVE_ORDER:
            return []
        positions = self.broker.fetch_positions()
        if not positions:
            return []
        unprotected = []
        for position in positions:
            symbol = position["symbol"]
            orders = self.broker.get_open_orders(symbol)
            has_stop_loss = any(
                "STOP" in str(order.get("type") or "").upper()
                and str(
                    order.get("positionSide")
                    or (order.get("info") or {}).get("positionSide")
                    or ""
                ).upper() == position["side"]
                for order in orders
            )
            if not has_stop_loss:
                unprotected.append(f"{symbol} {position['side']}")
        return unprotected

    def _get_valid_ai_response(
        self,
        prompt_context: str,
        custom_instructions: str,
        result: dict,
    ) -> AIResponse:
        messages = [
            {
                "role": "system",
                "content": build_system_prompt(self.config),
            },
            {
                "role": "user",
                "content": build_user_prompt(prompt_context, custom_instructions),
            },
        ]
        errors = []
        for attempt in range(self.config.AI_MAX_RETRIES + 1):
            try:
                ai_response = self.ai_agent.analyze_with_messages(messages)
                result["tokens_used"] += ai_response.usage.get("total_tokens", 0)
                self.risk_engine.validate_batch(
                    ai_response.tool_calls, self.broker, result["market_context"]
                )
                result["validation_retries"] = attempt
                return ai_response
            except (AIAgentError, RiskValidationError) as exc:
                errors.append(str(exc))
                if attempt >= self.config.AI_MAX_RETRIES:
                    break
                messages.append({
                    "role": "user",
                    "content": f"上一次响应未执行，原因：{exc}\n请重新生成完整且合规的工具调用。",
                })
        raise RuntimeError("模型决策未通过校验: " + "；".join(errors))

    def run_cycle(self) -> dict:
        """执行一个互斥、可追溯且不重复交易的决策周期。"""
        if not self._cycle_lock.acquire(blocking=False):
            raise RuntimeError("已有交易周期正在执行")
        cycle_id = str(uuid4())
        result = {
            "cycle_id": cycle_id,
            "timestamp": now_with_tz().isoformat(),
            "success": False,
            "error": None,
            "actions": [],
            "memory_updated": False,
            "tokens_used": 0,
            "trading_mode": self.trading_mode,
            "live_trading": self.live_trading,
            "validation_retries": 0,
            "recovery": {"resolved": [], "pending": []},
        }
        cycle = TradingCycle(
            cycle_id=cycle_id,
            trading_mode=self.trading_mode,
            status="RUNNING",
        )
        try:
            db.session.add(cycle)
            db.session.commit()
            if not self.live_trading:
                triggered = self.paper_broker.process_pending_orders(
                    self.data_engine.symbols
                )
                result["paper_triggers"] = len(triggered)

            result["recovery"] = DecisionRecovery(self).run()

            memory = self._get_memory_content()
            custom_instructions = self._get_custom_instructions()
            context = self.data_engine.aggregate(
                memory,
                account_provider=self.broker,
                trading_mode=self.trading_mode,
            )
            prompt_context = self.data_engine.build_prompt_context(context)
            try:
                unprotected = self._unprotected_positions()
            except Exception as exc:
                logger.warning("决策前保护单核对失败: %s", exc)
            else:
                if unprotected:
                    prompt_context += (
                        "\n以下当前持仓未检测到止损，请结合行情评估并修复保护："
                        + "、".join(unprotected)
                    )
            pending = result["recovery"]["pending"]
            if pending:
                by_symbol = {}
                for issue in pending:
                    summary = by_symbol.setdefault(issue["symbol"], {
                        "blocked_tools": set(), "errors": set(),
                    })
                    summary["blocked_tools"].update(issue["blocked_tools"])
                    summary["errors"].add(issue["error"][:300])
                summaries = [{
                    "symbol": symbol, "blocked_tools": sorted(value["blocked_tools"]),
                    "errors": sorted(value["errors"])[:3],
                } for symbol, value in by_symbol.items()]
                prompt_context += (
                    "\n自动修复报告（不是交易指令）：\n"
                    + json.dumps(summaries, ensure_ascii=False)[:8000]
                    + "\n请根据最新持仓处理缺失保护、失效触发价及撤单问题，"
                    "避免 blocked_tools 中列出的冲突动作；其他交易对继续正常决策。"
                )
            previous = TradingCycle.query.filter(
                TradingCycle.trading_mode == self.trading_mode,
                TradingCycle.cycle_id != cycle_id,
            ).order_by(TradingCycle.started_at.desc()).first()
            if previous and previous.error:
                prompt_context += "\n上一周期未完成原因，请结合最新状态调整：" + previous.error[:4000]
            snapshot = self._save_snapshot(context)
            result["market_context"] = context
            ai_response = self._get_valid_ai_response(
                prompt_context, custom_instructions, result
            )

            refresh_needed = False
            all_success = True
            for index, tool_call in enumerate(ai_response.tool_calls):
                client_order_id = self._client_order_id(cycle_id, index, tool_call.name)
                decision = self._save_decision_intent(
                    cycle_id,
                    tool_call,
                    ai_response.reasoning,
                    snapshot,
                    client_order_id,
                )
                conflict = next((item for item in pending
                                 if item["symbol"] == tool_call.args.get("target")
                                 and tool_call.name in item["blocked_tools"]), None)
                if conflict:
                    execution = ExecutionResult(
                        False,
                        status="SKIPPED",
                        symbol=tool_call.args.get("target", ""),
                        error=f"该交易对存在未确认操作，等待自动对账: {conflict['error']}",
                    )
                    success = False
                else:
                    try:
                        if refresh_needed and tool_call.name != "update_memory":
                            self.risk_engine.validate_batch([tool_call], self.broker, context)
                    except Exception as exc:
                        success, execution = False, ExecutionResult(
                            False, status="SKIPPED", error=f"最新状态校验失败: {exc}"
                        )
                    else:
                        try:
                            success, execution = self._execute_tool(tool_call, client_order_id)
                        except Exception as exc:
                            db.session.rollback()
                            success, execution = False, ExecutionResult(
                                False, status="FAILED" if tool_call.name == "update_memory" else "UNKNOWN",
                                error=str(exc),
                            )
                if not success:
                    all_success = False
                    refresh_needed = True
                if execution and execution.status in self.RECONCILIATION_STATUSES:
                    pending.append({
                        "decision_id": decision.id,
                        "symbol": tool_call.args.get("target", ""),
                        "tool": tool_call.name, "error": execution.error or execution.status,
                        "blocked_tools": sorted(MARKET_TOOLS | {"modify_position"}),
                    })
                if tool_call.name == "update_memory" and success:
                    result["memory_updated"] = True
                self._finalize_decision(decision, execution)
                result["actions"].append({
                    "tool": tool_call.name,
                    "info": tool_call.info,
                    "args": tool_call.args,
                    "success": success,
                    "status": execution.status if execution else "SUCCESS",
                    "error": execution.error if execution else None,
                    "executed": (
                        tool_call.name != "update_memory"
                        and (execution is None or execution.status != "SKIPPED")
                    ),
                })

            self._save_equity_snapshot()
            # 失保核对属于事后告警，其自身失败不应改变本周期的执行结论
            try:
                unprotected = self._unprotected_positions()
            except Exception as exc:
                result["protection_audit_error"] = str(exc)
                logger.error("持仓保护单核对失败: %s", exc)
            else:
                if unprotected:
                    result["unprotected_positions"] = unprotected
                    logger.warning(
                        "以下持仓当前没有止损保护: %s", "、".join(unprotected)
                    )
            result["success"] = all_success and result["memory_updated"] and not pending
            if not result["success"]:
                failures = [a["error"] for a in result["actions"] if a["error"]]
                failures.extend(item["error"] for item in pending)
                result["error"] = "；".join(failures) or "本周期记忆未更新"
            cycle.status = "SUCCESS" if result["success"] else "PARTIAL"
            cycle.error = result["error"]
            cycle.finished_at = utc_now()
            cycle.tokens_used = result["tokens_used"]
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.exception("交易周期失败 %s: %s", cycle_id, exc)
            result["error"] = str(exc)
            stored_cycle = db.session.get(TradingCycle, cycle_id)
            if stored_cycle:
                stored_cycle.status = "FAILED"
                stored_cycle.error = str(exc)
                stored_cycle.finished_at = utc_now()
                # 失败周期同样记录实际 token 用量，保留完整审计信息
                stored_cycle.tokens_used = result["tokens_used"]
                db.session.commit()
        finally:
            result.pop("market_context", None)
            self._cycle_lock.release()
        return result

    def close_all_positions(self) -> dict:
        """在当前交易模式下按方向平掉全部仓位。"""
        if not self._cycle_lock.acquire(blocking=False):
            raise RuntimeError("交易周期运行中，暂不能全平")
        results = {"closed": [], "cancelled": [], "errors": []}
        try:
            positions = self.broker.fetch_positions()
            for index, position in enumerate(positions):
                outcome = self.executor.close_position(
                    position["symbol"],
                    100,
                    reason="控制台一键全平",
                    position_side=position["side"],
                    client_order_id=self._client_order_id(
                        uuid4().hex, index, "close_all"
                    ),
                )
                if outcome.success:
                    results["closed"].append({
                        "symbol": position["symbol"],
                        "side": position["side"],
                        "quantity": outcome.quantity,
                        "order_id": outcome.order_id,
                    })
                else:
                    results["errors"].append({
                        "symbol": position["symbol"],
                        "side": position["side"],
                        "error": outcome.error,
                        "status": outcome.status,
                    })
            symbols = set(self.data_engine.symbols)
            symbols.update(position["symbol"] for position in positions)
            for symbol in sorted(symbols):
                outcome = self.executor.cancel_orders(symbol, "all")
                if outcome.success:
                    cancelled_count = int(outcome.quantity or 0)
                    if cancelled_count:
                        results["cancelled"].append({
                            "symbol": symbol,
                            "count": cancelled_count,
                        })
                else:
                    results["errors"].append({
                        "symbol": symbol,
                        "operation": "cancel_orders",
                        "error": outcome.error,
                        "status": outcome.status,
                    })
            return results
        finally:
            self._cycle_lock.release()

    def get_status(self) -> dict:
        return {
            "symbols": self.data_engine.symbols,
            "has_custom_instructions": bool(self._get_custom_instructions()),
            "memory_length": len(self._get_memory_content()),
            "trading_mode": self.trading_mode,
            "live_trading": self.live_trading,
            "ai_connected": bool(self.ai_agent.api_key),
        }
