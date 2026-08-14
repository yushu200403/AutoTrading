"""交易周期协调器。"""

import json
import logging
from datetime import timedelta
from decimal import Decimal
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
from app.bot.exceptions import (
    ReconciliationRequiredError,
    RiskLimitBreachedError,
)
from app.bot.executor import ExecutionResult, TradeExecutor
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
    # 任一状态残留时必须先人工对账，禁止继续交易。
    RECONCILIATION_STATUSES = ("PENDING", "UNKNOWN", "PARTIAL", "CRITICAL")

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
        )
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

    def _check_account_limits(self, context: MarketContext) -> None:
        """校验模型预算与账户级熔断，触发时暂停交易并要求人工介入。"""
        config = self.config
        if config.AI_MAX_DAILY_TOKENS:
            spent = TradingCycle.tokens_used_since(utc_now() - timedelta(hours=24))
            if spent >= config.AI_MAX_DAILY_TOKENS:
                raise RiskLimitBreachedError(
                    f"最近 24 小时模型 token 用量 {spent} 已达上限 "
                    f"{config.AI_MAX_DAILY_TOKENS}"
                )
        total_equity = Decimal(str(context.account_balance.get("total", 0) or 0))
        if total_equity <= 0:
            return
        if config.RISK_MAX_DAILY_LOSS_PERCENT > 0:
            baseline = EquitySnapshot.get_24h_ago(self.trading_mode)
            if baseline is not None and Decimal(str(baseline.total_equity)) > 0:
                previous = Decimal(str(baseline.total_equity))
                loss_percent = (previous - total_equity) / previous * 100
                if loss_percent >= config.RISK_MAX_DAILY_LOSS_PERCENT:
                    raise RiskLimitBreachedError(
                        f"最近 24 小时净值回落 {loss_percent:.2f}%，已达日亏损上限 "
                        f"{config.RISK_MAX_DAILY_LOSS_PERCENT}%"
                    )
        if config.RISK_MAX_DRAWDOWN_PERCENT > 0:
            peak = EquitySnapshot.get_peak_equity(self.trading_mode)
            if peak is not None and Decimal(str(peak)) > 0:
                peak_equity = Decimal(str(peak))
                drawdown = (peak_equity - total_equity) / peak_equity * 100
                if drawdown >= config.RISK_MAX_DRAWDOWN_PERCENT:
                    raise RiskLimitBreachedError(
                        f"净值自峰值回撤 {drawdown:.2f}%，已达回撤上限 "
                        f"{config.RISK_MAX_DRAWDOWN_PERCENT}%"
                    )

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
            "halt_required": False,
        }
        cycle = TradingCycle(
            cycle_id=cycle_id,
            trading_mode=self.trading_mode,
            status="RUNNING",
        )
        try:
            db.session.add(cycle)
            db.session.commit()
            unresolved = TradeDecision.query.filter(
                TradeDecision.trading_mode == self.trading_mode,
                TradeDecision.execution_status.in_(self.RECONCILIATION_STATUSES),
            ).order_by(TradeDecision.id).first()
            if unresolved is not None:
                raise ReconciliationRequiredError(
                    unresolved.id, unresolved.execution_status, self.trading_mode
                )
            if not self.live_trading:
                triggered = self.paper_broker.process_pending_orders(
                    self.data_engine.symbols
                )
                result["paper_triggers"] = len(triggered)

            memory = self._get_memory_content()
            custom_instructions = self._get_custom_instructions()
            context = self.data_engine.aggregate(
                memory,
                account_provider=self.broker,
                trading_mode=self.trading_mode,
            )
            self._check_account_limits(context)
            prompt_context = self.data_engine.build_prompt_context(context)
            snapshot = self._save_snapshot(context)
            result["market_context"] = context
            ai_response = self._get_valid_ai_response(
                prompt_context, custom_instructions, result
            )

            block_risk_actions = False
            all_success = True
            for index, tool_call in enumerate(ai_response.tool_calls):
                decision = self._save_decision_intent(
                    cycle_id,
                    tool_call,
                    ai_response.reasoning,
                    snapshot,
                )
                is_risk_action = tool_call.name not in {
                    "update_memory", "cancel_orders", "cancel_order", "close_position"
                }
                if block_risk_actions and is_risk_action:
                    execution = ExecutionResult(
                        False,
                        status="SKIPPED",
                        symbol=tool_call.args.get("target", ""),
                        error="前序工具失败，已阻止后续增险动作",
                    )
                    success = False
                else:
                    success, execution = self._execute_tool(
                        tool_call,
                        self._client_order_id(cycle_id, index, tool_call.name),
                    )
                if not success:
                    all_success = False
                    block_risk_actions = True
                if tool_call.name == "update_memory" and success:
                    result["memory_updated"] = True
                self._finalize_decision(decision, execution)
                result["actions"].append({
                    "tool": tool_call.name,
                    "info": tool_call.info,
                    "args": tool_call.args,
                    "success": success,
                    "status": execution.status if execution else "SUCCESS",
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
            result["success"] = all_success and result["memory_updated"]
            if not result["success"]:
                result["error"] = "一个或多个工具未成功执行"
            cycle.status = "SUCCESS" if result["success"] else "PARTIAL"
            cycle.finished_at = utc_now()
            cycle.tokens_used = result["tokens_used"]
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.exception("交易周期失败 %s: %s", cycle_id, exc)
            result["error"] = str(exc)
            if isinstance(exc, (ReconciliationRequiredError, RiskLimitBreachedError)):
                result["halt_required"] = True
                logger.critical("交易已暂停，需人工处理: %s", exc)
            stored_cycle = db.session.get(TradingCycle, cycle_id)
            if stored_cycle:
                stored_cycle.status = "FAILED"
                stored_cycle.error = str(exc)
                stored_cycle.finished_at = utc_now()
                # 失败周期同样可能已消耗 token，必须计入预算
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
