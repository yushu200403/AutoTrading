"""交易订单编排器。"""

import hashlib
import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional

from app.bot.exceptions import OrderResultUnknownError, PositionNotFoundError
from config import get_config


logger = logging.getLogger(__name__)


def _finite_float(value, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是有效数字") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} 必须是有限数字")
    return result


@dataclass
class ExecutionResult:
    """一次工具执行的结构化结果。"""

    success: bool
    status: str = "FAILED"
    order_id: Optional[str] = None
    symbol: str = ""
    side: str = ""
    quantity: float = 0.0
    executed_price: float = 0.0
    error: Optional[str] = None
    sl_order_id: Optional[str] = None
    tp_order_id: Optional[str] = None


def _child_order_id(base: Optional[str], suffix: str) -> str:
    """生成符合币安 36 字符限制的稳定客户端订单 ID。

    同一 base 与 suffix 必须得到同一 ID，幂等重放才能命中既有订单。
    因此 base 缺失时不能退化为固定值，否则不同订单会共用一个 ID。
    """
    if not base:
        raise ValueError("生成子订单 ID 需要父级客户端订单 ID")
    digest = hashlib.sha256(f"{base}:{suffix}".encode()).hexdigest()[:28]
    return f"onf-{digest}"


class TradeExecutor:
    """对真实或模拟 Broker 执行相同的订单编排。"""

    def __init__(self, broker, config=None):
        self.client = broker
        self.config = config or get_config()

    def _current_price(self, symbol: str) -> float:
        ticker = self.client.fetch_ticker(symbol)
        price = _finite_float(ticker.last_price, f"{symbol} 当前价格")
        if price <= 0:
            raise ValueError(f"{symbol} 当前价格无效")
        return price

    def _validate_protective_prices(
        self,
        symbol: str,
        position_side: str,
        stop_loss_price: Optional[float],
        take_profit_price: Optional[float],
    ) -> None:
        current = self._current_price(symbol)
        distance_percent = float(self.config.RISK_MIN_PROTECTIVE_DISTANCE_PERCENT)
        min_distance = current * distance_percent / 100
        if stop_loss_price is not None:
            valid = (
                stop_loss_price < current
                if position_side == "LONG"
                else stop_loss_price > current
            )
            if not valid:
                raise ValueError("止损价与当前价格及仓位方向不匹配")
            if abs(current - stop_loss_price) < min_distance:
                raise ValueError(
                    f"止损价与现价间距不足 {distance_percent}%，会在建仓后立刻触发"
                )
        if take_profit_price is not None:
            valid = (
                take_profit_price > current
                if position_side == "LONG"
                else take_profit_price < current
            )
            if not valid:
                raise ValueError("止盈价与当前价格及仓位方向不匹配")
            if abs(take_profit_price - current) < min_distance:
                raise ValueError(
                    f"止盈价与现价间距不足 {distance_percent}%，会在建仓后立刻触发"
                )

    def open_position(
        self,
        symbol: str,
        side: str,
        amount_usdt: float,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> ExecutionResult:
        """开仓或加仓；保护单失败时立即回补本次新增数量。"""
        position_side = side.upper()
        if position_side not in {"LONG", "SHORT"}:
            return ExecutionResult(False, error=f"无效方向: {side}", symbol=symbol)
        order_side = "BUY" if position_side == "LONG" else "SELL"
        opposite_side = "SELL" if order_side == "BUY" else "BUY"

        try:
            amount_usdt = _finite_float(amount_usdt, "交易金额")
            if amount_usdt <= 0:
                raise ValueError("交易金额必须大于 0")
            minimum = self.client.get_min_notional(symbol)
            if amount_usdt < minimum:
                raise ValueError(f"交易金额低于交易所最小名义价值 {minimum}")
            self._validate_protective_prices(
                symbol, position_side, stop_loss_price, take_profit_price
            )
            quantity = _finite_float(
                self.client.calculate_quantity(symbol, amount_usdt), "下单数量"
            )
            if quantity <= 0:
                raise ValueError("按交易所精度计算后的数量为 0")

            order = self.client.create_market_order(
                symbol,
                order_side,
                quantity,
                position_side,
                client_order_id=client_order_id,
            )
            executed_price = _finite_float(
                order.get("average") or order.get("price") or 0,
                "开仓成交价格",
            )
            created_protection_ids = []
            sl_order_id = None
            tp_order_id = None
            try:
                if stop_loss_price is not None:
                    sl_order = self.client.create_stop_loss_order(
                        symbol,
                        opposite_side,
                        quantity,
                        stop_loss_price,
                        position_side,
                        client_order_id=_child_order_id(client_order_id, "sl"),
                    )
                    sl_order_id = sl_order.get("id")
                    created_protection_ids.append(sl_order_id)
                if take_profit_price is not None:
                    tp_order = self.client.create_take_profit_order(
                        symbol,
                        opposite_side,
                        quantity,
                        take_profit_price,
                        position_side,
                        client_order_id=_child_order_id(client_order_id, "tp"),
                    )
                    tp_order_id = tp_order.get("id")
                    created_protection_ids.append(tp_order_id)
            except Exception as protection_error:
                cleanup_errors = self._cancel_created_orders(
                    symbol,
                    [{"id": order_id} for order_id in created_protection_ids],
                )
                rollback_error = self._rollback_open_quantity(
                    symbol,
                    position_side,
                    opposite_side,
                    quantity,
                    client_order_id,
                )
                message = f"保护单创建失败，已回补新增仓位: {protection_error}"
                status = "COMPENSATED"
                if rollback_error:
                    message = f"保护单创建失败且仓位回补失败: {rollback_error}"
                    status = "CRITICAL"
                if cleanup_errors:
                    message += "；保护单清理失败: " + "；".join(cleanup_errors)
                    status = "CRITICAL"
                return ExecutionResult(
                    False,
                    status=status,
                    order_id=order.get("id"),
                    symbol=symbol,
                    side=position_side,
                    quantity=quantity,
                    executed_price=executed_price,
                    error=message,
                    sl_order_id=sl_order_id,
                    tp_order_id=tp_order_id,
                )

            return ExecutionResult(
                True,
                status="SUCCESS",
                order_id=order.get("id"),
                symbol=symbol,
                side=position_side,
                quantity=quantity,
                executed_price=executed_price,
                sl_order_id=sl_order_id,
                tp_order_id=tp_order_id,
            )
        except OrderResultUnknownError as exc:
            logger.critical("订单结果未知，禁止自动重试: %s", exc)
            return ExecutionResult(
                False,
                status="UNKNOWN",
                symbol=symbol,
                side=position_side,
                error=str(exc),
            )
        except Exception as exc:
            logger.error("开仓失败 %s %s: %s", symbol, position_side, exc)
            return ExecutionResult(
                False,
                status="FAILED",
                symbol=symbol,
                side=position_side,
                error=str(exc),
            )

    def _rollback_open_quantity(
        self,
        symbol: str,
        position_side: str,
        close_side: str,
        quantity: float,
        parent_client_order_id: Optional[str],
    ) -> Optional[str]:
        try:
            self.client.create_market_order(
                symbol,
                close_side,
                quantity,
                position_side,
                client_order_id=_child_order_id(parent_client_order_id, "rollback"),
            )
            return None
        except Exception as exc:
            logger.critical("新增仓位回补失败 %s %s: %s", symbol, position_side, exc)
            return str(exc)

    def close_position(
        self,
        symbol: str,
        percentage: int,
        reason: str = "",
        position_side: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> ExecutionResult:
        """按明确仓位方向平仓或减仓。"""
        try:
            if not 1 <= percentage <= 100:
                raise ValueError("平仓百分比必须在 1 到 100 之间")
            position = self.client.get_position_size(symbol, position_side)
            if position is None:
                raise PositionNotFoundError(symbol)
            resolved_side = position["side"]
            current_contracts = _finite_float(position["contracts"], "当前仓位数量")
            close_quantity = _finite_float(
                self.client.amount_to_precision(
                    symbol, current_contracts * percentage / 100
                ),
                "平仓数量",
            )
            if close_quantity <= 0:
                raise ValueError("按交易所精度计算后的平仓数量为 0")
            close_side = "SELL" if resolved_side == "LONG" else "BUY"

            order = self.client.create_market_order(
                symbol,
                close_side,
                close_quantity,
                resolved_side,
                client_order_id=client_order_id,
            )
            executed_price = _finite_float(
                order.get("average") or order.get("price") or 0,
                "平仓成交价格",
            )
            try:
                if percentage == 100:
                    self._cancel_position_orders(symbol, resolved_side)
                else:
                    resize_result = self._resize_after_partial_close(
                        symbol,
                        resolved_side,
                        client_order_id,
                    )
                    if resize_result is not None and not resize_result.success:
                        status = (
                            "CRITICAL"
                            if resize_result.status == "CRITICAL"
                            else "PARTIAL"
                        )
                        return ExecutionResult(
                            False,
                            status=status,
                            order_id=order.get("id"),
                            symbol=symbol,
                            side=resolved_side,
                            quantity=close_quantity,
                            executed_price=executed_price,
                            error=(
                                "部分平仓已成交，但保护单数量同步失败: "
                                f"{resize_result.error}"
                            ),
                        )
            except Exception as cleanup_error:
                return ExecutionResult(
                    False,
                    status="PARTIAL",
                    order_id=order.get("id"),
                    symbol=symbol,
                    side=resolved_side,
                    quantity=close_quantity,
                    executed_price=executed_price,
                    error=f"平仓已成交，但保护单清理失败: {cleanup_error}",
                )
            return ExecutionResult(
                True,
                status="SUCCESS",
                order_id=order.get("id"),
                symbol=symbol,
                side=resolved_side,
                quantity=close_quantity,
                executed_price=executed_price,
            )
        except OrderResultUnknownError as exc:
            return ExecutionResult(
                False,
                status="UNKNOWN",
                symbol=symbol,
                side=position_side or "",
                error=str(exc),
            )
        except Exception as exc:
            logger.error("平仓失败 %s: %s", symbol, exc)
            return ExecutionResult(
                False,
                status="FAILED",
                symbol=symbol,
                side=position_side or "",
                error=str(exc),
            )

    def _cancel_position_orders(self, symbol: str, position_side: str) -> None:
        if hasattr(self.client, "cancel_position_orders"):
            self.client.cancel_position_orders(symbol, position_side)
        else:
            self.client.cancel_all_orders(symbol)

    def _resize_after_partial_close(
        self,
        symbol: str,
        position_side: str,
        parent_client_order_id: Optional[str],
    ) -> Optional[ExecutionResult]:
        orders = self._matching_protective_orders(symbol, position_side)
        if not orders:
            return None
        stop_loss_price = None
        take_profit_price = None
        for order in orders:
            order_type = str(order.get("type") or order.get("order_type") or "").upper()
            info = order.get("info") or {}
            raw_trigger = order.get("stopPrice") or info.get("triggerPrice")
            if raw_trigger is None:
                raise ValueError(f"保护单 {order.get('id')} 缺少触发价")
            trigger = _finite_float(raw_trigger, "保护单触发价")
            if "TAKE_PROFIT" in order_type and take_profit_price is None:
                take_profit_price = trigger
            elif "STOP" in order_type and stop_loss_price is None:
                stop_loss_price = trigger
        if stop_loss_price is None and take_profit_price is None:
            return None
        return self.modify_position_tpsl(
            symbol,
            stop_loss_price,
            take_profit_price,
            position_side=position_side,
            client_order_id=_child_order_id(parent_client_order_id, "resize"),
        )

    def set_leverage(self, symbol: str, leverage: int) -> ExecutionResult:
        try:
            self.client.set_leverage(symbol, leverage)
            return ExecutionResult(
                True, status="SUCCESS", symbol=symbol, side=f"LEVERAGE_{leverage}x"
            )
        except Exception as exc:
            return ExecutionResult(False, symbol=symbol, error=str(exc))

    def set_margin_mode(self, symbol: str, mode: str) -> ExecutionResult:
        try:
            self.client.set_margin_mode(symbol, mode)
            return ExecutionResult(
                True, status="SUCCESS", symbol=symbol, side=f"MARGIN_{mode.upper()}"
            )
        except Exception as exc:
            return ExecutionResult(False, symbol=symbol, error=str(exc))

    def modify_position_tpsl(
        self,
        symbol: str,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        position_side: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> ExecutionResult:
        """先创建新保护单，成功后再撤销旧保护单。"""
        try:
            position = self.client.get_position_size(symbol, position_side)
            if position is None:
                raise PositionNotFoundError(symbol)
            resolved_side = position["side"]
            quantity = _finite_float(position["contracts"], "当前仓位数量")
            opposite_side = "SELL" if resolved_side == "LONG" else "BUY"
            self._validate_protective_prices(
                symbol, resolved_side, stop_loss_price, take_profit_price
            )
            old_orders = self._matching_protective_orders(symbol, resolved_side)
            created = []
            try:
                if stop_loss_price is not None:
                    order = self.client.create_stop_loss_order(
                        symbol,
                        opposite_side,
                        quantity,
                        stop_loss_price,
                        resolved_side,
                        client_order_id=_child_order_id(client_order_id, "replace-sl"),
                    )
                    created.append(order)
                if take_profit_price is not None:
                    order = self.client.create_take_profit_order(
                        symbol,
                        opposite_side,
                        quantity,
                        take_profit_price,
                        resolved_side,
                        client_order_id=_child_order_id(client_order_id, "replace-tp"),
                    )
                    created.append(order)
            except Exception as create_error:
                cleanup_errors = self._cancel_created_orders(symbol, created)
                status = "COMPENSATED" if not cleanup_errors else "CRITICAL"
                detail = f"新保护单创建失败: {create_error}"
                if cleanup_errors:
                    detail += "；已创建保护单清理失败: " + "；".join(cleanup_errors)
                return ExecutionResult(
                    False,
                    status=status,
                    symbol=symbol,
                    side=resolved_side,
                    error=detail,
                )

            replaced_types = {
                "STOP" if stop_loss_price is not None else "",
                "TAKE_PROFIT" if take_profit_price is not None else "",
            }
            cancelled_old_count = 0
            for old in old_orders:
                order_type = str(old.get("type") or old.get("order_type") or "").upper()
                if any(marker and marker in order_type for marker in replaced_types):
                    try:
                        result = self.client.cancel_order_by_id(symbol, str(old["id"]))
                    except OrderResultUnknownError as exc:
                        return ExecutionResult(
                            False,
                            status="CRITICAL",
                            symbol=symbol,
                            side=resolved_side,
                            error=(
                                f"旧保护单 {old['id']} 撤销结果未知，"
                                f"保护单可能重复: {exc}"
                            ),
                        )
                    if not result.get("success"):
                        cleanup_errors = []
                        if cancelled_old_count == 0:
                            cleanup_errors = self._cancel_created_orders(symbol, created)
                        if cancelled_old_count == 0 and not cleanup_errors:
                            status = "COMPENSATED"
                            detail = f"旧保护单撤销失败，已撤销新保护单: {old['id']}"
                        else:
                            status = "CRITICAL"
                            detail = f"旧保护单撤销失败，保护单可能部分重复: {old['id']}"
                            if cleanup_errors:
                                detail += "；新保护单清理失败: " + "；".join(cleanup_errors)
                        return ExecutionResult(
                            False,
                            status=status,
                            symbol=symbol,
                            side=resolved_side,
                            error=detail,
                        )
                    cancelled_old_count += 1

            return ExecutionResult(
                True,
                status="SUCCESS",
                symbol=symbol,
                side=resolved_side,
                sl_order_id=next(
                    (o.get("id") for o in created if "STOP" in str(o.get("type", ""))),
                    None,
                ),
                tp_order_id=next(
                    (o.get("id") for o in created if "TAKE_PROFIT" in str(o.get("type", ""))),
                    None,
                ),
            )
        except Exception as exc:
            logger.error("修改保护单失败 %s: %s", symbol, exc)
            return ExecutionResult(
                False,
                status="FAILED",
                symbol=symbol,
                side=position_side or "",
                error=str(exc),
            )

    def _cancel_created_orders(self, symbol: str, orders: List[Dict]) -> List[str]:
        errors = []
        for order in orders:
            order_id = order.get("id")
            if not order_id:
                errors.append("新保护单缺少订单 ID")
                continue
            try:
                result = self.client.cancel_order_by_id(symbol, str(order_id))
                if not result.get("success"):
                    errors.append(str(result.get("error") or order_id))
            except Exception as exc:
                errors.append(str(exc))
        return errors

    def _matching_protective_orders(self, symbol: str, position_side: str) -> List[Dict]:
        orders = self.client.get_open_orders(symbol)
        result = []
        for order in orders:
            order_type = str(order.get("type") or "").upper()
            raw_side = str(
                order.get("positionSide")
                or (order.get("info") or {}).get("positionSide")
                or ""
            ).upper()
            if "STOP" not in order_type and "TAKE_PROFIT" not in order_type:
                continue
            if not raw_side:
                raise RuntimeError(f"保护单 {order.get('id')} 缺少持仓方向")
            if raw_side == position_side:
                result.append(order)
        return result

    def cancel_orders(self, symbol: str, order_type: str = "all") -> ExecutionResult:
        try:
            cancelled = self.client.cancel_orders_by_type(symbol, order_type)
            return ExecutionResult(
                True,
                status="SUCCESS",
                symbol=symbol,
                side=f"CANCEL_{order_type.upper()}",
                quantity=float(len(cancelled)),
            )
        except OrderResultUnknownError as exc:
            logger.critical("撤单结果未知，需人工核对: %s", exc)
            return ExecutionResult(
                False, status="UNKNOWN", symbol=symbol, error=str(exc)
            )
        except Exception as exc:
            return ExecutionResult(False, symbol=symbol, error=str(exc))

    def cancel_order_by_id(self, symbol: str, order_id: str) -> ExecutionResult:
        try:
            result = self.client.cancel_order_by_id(symbol, order_id)
            if not result.get("success"):
                return ExecutionResult(
                    False,
                    symbol=symbol,
                    error=result.get("error", "订单撤销失败"),
                )
            return ExecutionResult(
                True,
                status="SUCCESS",
                symbol=symbol,
                side=f"CANCEL_ORDER_{result.get('type', 'unknown').upper()}",
                order_id=order_id,
            )
        except OrderResultUnknownError as exc:
            logger.critical("撤单结果未知，需人工核对: %s", exc)
            return ExecutionResult(
                False, status="UNKNOWN", symbol=symbol, error=str(exc)
            )
        except Exception as exc:
            return ExecutionResult(False, symbol=symbol, error=str(exc))
