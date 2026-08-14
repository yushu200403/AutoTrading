"""真实行情驱动的持久化模拟交易执行器。"""

import logging
from decimal import Decimal, InvalidOperation
from threading import RLock
from typing import Dict, List, Optional
from uuid import uuid4

from app import db
from app.models import (
    PaperAccount,
    PaperExecution,
    PaperOrder,
    PaperPosition,
    PaperSymbolSetting,
    utc_now,
)


logger = logging.getLogger(__name__)


def _decimal(value) -> Decimal:
    """将外部数值安全转换为有限 Decimal。"""
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"无效数值: {value}") from exc
    if not result.is_finite():
        raise ValueError(f"数值必须是有限值: {value}")
    return result


class PaperBroker:
    """实现与交易执行器兼容的模拟经纪商接口。"""

    def __init__(self, market_client, config):
        self.market = market_client
        self.config = config
        self._lock = RLock()

    def _account(self) -> PaperAccount:
        account = db.session.get(PaperAccount, 1)
        if account is None:
            account = PaperAccount(
                id=1,
                wallet_balance=self.config.PAPER_INITIAL_BALANCE_USDT,
            )
            db.session.add(account)
            db.session.flush()
        return account

    def _setting(self, symbol: str) -> PaperSymbolSetting:
        setting = PaperSymbolSetting.query.filter_by(symbol=symbol).first()
        if setting is None:
            setting = PaperSymbolSetting(
                symbol=symbol,
                leverage=self.config.PAPER_DEFAULT_LEVERAGE,
                margin_mode="cross",
            )
            db.session.add(setting)
            db.session.flush()
        return setting

    def _price(self, symbol: str) -> Decimal:
        ticker = self.market.fetch_ticker(symbol)
        price = _decimal(ticker.last_price)
        if price <= 0:
            raise ValueError(f"{symbol} 的真实行情价格无效")
        return price

    def _position_price_map(self, positions: List[PaperPosition]) -> Dict[str, Decimal]:
        prices = {}
        for position in positions:
            if position.symbol not in prices:
                prices[position.symbol] = self._price(position.symbol)
        return prices

    @staticmethod
    def _unrealized(position: PaperPosition, mark_price: Decimal) -> Decimal:
        quantity = _decimal(position.quantity)
        entry = _decimal(position.entry_price)
        direction = Decimal("1") if position.side == "LONG" else Decimal("-1")
        return (mark_price - entry) * quantity * direction

    def _balance_values(self):
        account = self._account()
        positions = PaperPosition.query.filter(PaperPosition.quantity > 0).all()
        prices = self._position_price_map(positions)
        margin = Decimal("0")
        unrealized = Decimal("0")
        for position in positions:
            quantity = _decimal(position.quantity)
            entry = _decimal(position.entry_price)
            leverage = Decimal(str(position.leverage))
            margin += quantity * entry / leverage
            unrealized += self._unrealized(position, prices[position.symbol])
        wallet = _decimal(account.wallet_balance)
        return {
            "wallet": wallet,
            "margin": margin,
            "unrealized": unrealized,
            "total": wallet + unrealized,
            "free": wallet + unrealized - margin,
        }

    def fetch_balance(self) -> Dict[str, float]:
        """返回模拟账户余额，行情和未实现盈亏均实时计算。

        首次访问需要落地账户行，因此这里只 flush 而不 commit，
        避免这个只读接口顺带提交调用方尚未完成的事务。
        """
        with self._lock:
            try:
                values = self._balance_values()
                db.session.flush()
                return {
                    "total": float(values["total"]),
                    "free": float(values["free"]),
                    "used": float(values["margin"]),
                    "wallet": float(values["wallet"]),
                    "unrealized_pnl": float(values["unrealized"]),
                }
            except Exception:
                db.session.rollback()
                raise

    def fetch_positions(self, symbols: List[str] = None) -> List[Dict]:
        """返回模拟双向持仓。"""
        with self._lock:
            positions, prices = self._load_positions_with_prices(symbols)
        return self._describe_positions(positions, prices)

    def _load_positions_with_prices(self, symbols: List[str] = None):
        """在持锁状态下读取持仓，避免与下单路径看到中间状态。"""
        query = PaperPosition.query.filter(PaperPosition.quantity > 0)
        if symbols:
            query = query.filter(PaperPosition.symbol.in_(symbols))
        positions = query.order_by(PaperPosition.symbol, PaperPosition.side).all()
        return positions, self._position_price_map(positions)

    def _describe_positions(
        self, positions: List[PaperPosition], prices: Dict[str, Decimal]
    ) -> List[Dict]:
        result = []
        for position in positions:
            quantity = _decimal(position.quantity)
            entry = _decimal(position.entry_price)
            mark = prices[position.symbol]
            unrealized = self._unrealized(position, mark)
            margin = quantity * entry / Decimal(str(position.leverage))
            percentage = unrealized / margin * 100 if margin > 0 else Decimal("0")
            result.append({
                "symbol": position.symbol,
                "side": position.side,
                "contracts": float(quantity),
                "notional": float(quantity * mark),
                "entry_price": float(entry),
                "mark_price": float(mark),
                "unrealized_pnl": float(unrealized),
                "percentage": float(percentage),
                "leverage": position.leverage,
                "margin_mode": position.margin_mode,
            })
        return result

    def get_position_size(
        self, symbol: str, position_side: Optional[str] = None
    ) -> Optional[Dict]:
        """返回单个方向的模拟持仓，一次查询内完成判定与取价。"""
        with self._lock:
            positions, prices = self._load_positions_with_prices([symbol])
            if position_side:
                target = position_side.upper()
                positions = [item for item in positions if item.side == target]
            if not positions:
                return None
            if len(positions) > 1:
                raise ValueError(f"{symbol} 同时存在双向仓位，必须明确指定 side")
            return self._describe_positions(positions, prices)[0]

    def get_precision(self, symbol: str):
        return self.market.get_precision(symbol)

    def fetch_ticker(self, symbol: str):
        """透传真实行情。"""
        return self.market.fetch_ticker(symbol)

    def amount_to_precision(self, symbol: str, value: float) -> float:
        return self.market.amount_to_precision(symbol, value)

    def price_to_precision(self, symbol: str, value: float) -> float:
        return self.market.price_to_precision(symbol, value)

    def truncate_to_precision(self, value: float, precision: int) -> float:
        return self.market.truncate_to_precision(value, precision)

    def get_min_notional(self, symbol: str) -> float:
        return self.market.get_min_notional(symbol)

    def calculate_quantity(
        self, symbol: str, usdt_amount: float, current_price: float = None
    ) -> float:
        return self.market.calculate_quantity(symbol, usdt_amount, current_price)

    def get_fees(self, symbol: str) -> Dict[str, float]:
        return {
            "maker": float(self.config.PAPER_TAKER_FEE_RATE),
            "taker": float(self.config.PAPER_TAKER_FEE_RATE),
        }

    def create_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        position_side: str,
        client_order_id: Optional[str] = None,
        source_order_id: Optional[str] = None,
    ) -> Dict:
        """按真实最新价模拟市价成交，并保证客户端订单 ID 幂等。"""
        side = side.upper()
        position_side = position_side.upper()
        client_order_id = client_order_id or f"paper-{uuid4().hex}"
        if side not in {"BUY", "SELL"} or position_side not in {"LONG", "SHORT"}:
            raise ValueError("模拟订单方向无效")
        requested_quantity = _decimal(quantity)
        if requested_quantity <= 0:
            raise ValueError("模拟订单数量必须大于 0")

        with self._lock:
            existing = PaperExecution.query.filter_by(
                client_order_id=client_order_id
            ).first()
            if existing:
                if not (
                    existing.symbol == symbol
                    and existing.side == side
                    and existing.position_side == position_side
                    and _decimal(existing.quantity) == requested_quantity
                ):
                    raise ValueError("客户端订单 ID 已被不同的模拟订单使用")
                return self._execution_result(existing)

            try:
                price = self._price(symbol)

                account = self._account()
                setting = self._setting(symbol)
                position = PaperPosition.query.filter_by(
                    symbol=symbol, side=position_side
                ).with_for_update().first()
                is_opening = (
                    position_side == "LONG" and side == "BUY"
                ) or (
                    position_side == "SHORT" and side == "SELL"
                )
                realized = Decimal("0")

                if is_opening:
                    fee = requested_quantity * price * self.config.PAPER_TAKER_FEE_RATE
                    values = self._balance_values()
                    required_margin = (
                        requested_quantity * price / Decimal(str(setting.leverage))
                    )
                    if required_margin + fee > values["free"]:
                        raise ValueError(
                            f"模拟账户可用余额不足，需要 {required_margin + fee:.4f} USDT"
                        )
                    if position is None:
                        position = PaperPosition(
                            symbol=symbol,
                            side=position_side,
                            quantity=requested_quantity,
                            entry_price=price,
                            leverage=setting.leverage,
                            margin_mode=setting.margin_mode,
                        )
                        db.session.add(position)
                    else:
                        old_quantity = _decimal(position.quantity)
                        new_quantity = old_quantity + requested_quantity
                        position.entry_price = (
                            old_quantity * _decimal(position.entry_price)
                            + requested_quantity * price
                        ) / new_quantity
                        position.quantity = new_quantity
                    account.wallet_balance = _decimal(account.wallet_balance) - fee
                    executed_quantity = requested_quantity
                else:
                    if position is None or _decimal(position.quantity) <= 0:
                        raise ValueError(f"模拟账户不存在 {symbol} {position_side} 仓位")
                    current_quantity = _decimal(position.quantity)
                    if requested_quantity > current_quantity:
                        raise ValueError(
                            f"模拟平仓数量 {requested_quantity} 超过持仓数量 {current_quantity}"
                        )
                    executed_quantity = requested_quantity
                    fee = executed_quantity * price * self.config.PAPER_TAKER_FEE_RATE
                    direction = Decimal("1") if position_side == "LONG" else Decimal("-1")
                    realized = (
                        price - _decimal(position.entry_price)
                    ) * executed_quantity * direction
                    remaining = current_quantity - executed_quantity
                    account.wallet_balance = (
                        _decimal(account.wallet_balance) + realized - fee
                    )
                    if remaining == 0:
                        db.session.delete(position)
                    else:
                        position.quantity = remaining
                        position.realized_pnl = _decimal(position.realized_pnl) + realized
                    self._resize_protective_orders(
                        symbol,
                        position_side,
                        remaining,
                        excluded_order_id=source_order_id,
                    )

                order_id = f"PM-{uuid4().hex}"
                execution = PaperExecution(
                    order_id=order_id,
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side=side,
                    position_side=position_side,
                    quantity=executed_quantity,
                    executed_price=price,
                    fee=fee,
                    realized_pnl=realized,
                )
                db.session.add(execution)
                db.session.commit()
                logger.info(
                    "模拟成交: %s %s %s %s @ %s",
                    symbol,
                    side,
                    position_side,
                    executed_quantity,
                    price,
                )
                return self._execution_result(execution)
            except Exception:
                db.session.rollback()
                raise

    @staticmethod
    def _execution_result(execution: PaperExecution) -> Dict:
        return {
            "id": execution.order_id,
            "clientOrderId": execution.client_order_id,
            "symbol": execution.symbol,
            "side": execution.side.lower(),
            "average": float(execution.executed_price),
            "price": float(execution.executed_price),
            "amount": float(execution.quantity),
            "filled": float(execution.quantity),
            "status": "closed",
            "paper": True,
        }

    def _create_conditional_order(
        self,
        order_type: str,
        symbol: str,
        side: str,
        quantity: float,
        trigger_price: float,
        position_side: str,
        client_order_id: Optional[str] = None,
    ) -> Dict:
        with self._lock:
            try:
                return self._create_conditional_order_locked(
                    order_type,
                    symbol,
                    side,
                    quantity,
                    trigger_price,
                    position_side,
                    client_order_id,
                )
            except Exception:
                db.session.rollback()
                raise

    def _create_conditional_order_locked(
        self,
        order_type: str,
        symbol: str,
        side: str,
        quantity: float,
        trigger_price: float,
        position_side: str,
        client_order_id: Optional[str] = None,
    ) -> Dict:
        position_side = position_side.upper()
        side = side.upper()
        requested = _decimal(quantity)
        trigger = _decimal(trigger_price)
        client_order_id = client_order_id or f"paper-cond-{uuid4().hex}"
        existing = PaperOrder.query.filter_by(client_order_id=client_order_id).first()
        if existing:
            if not (
                existing.order_type == order_type
                and existing.symbol == symbol
                and existing.side == side
                and existing.position_side == position_side
                and _decimal(existing.quantity) == requested
                and _decimal(existing.trigger_price) == trigger
            ):
                raise ValueError("客户端订单 ID 已被不同的模拟条件单使用")
            return self._paper_order_result(existing)

        position = PaperPosition.query.filter_by(
            symbol=symbol, side=position_side
        ).first()
        if position is None or _decimal(position.quantity) <= 0:
            raise ValueError(f"无法为不存在的 {symbol} {position_side} 仓位创建保护单")
        if requested <= 0 or requested > _decimal(position.quantity):
            raise ValueError("保护单数量必须大于 0 且不能超过仓位")
        current = self._price(symbol)
        if order_type == "STOP_MARKET":
            valid = trigger < current if position_side == "LONG" else trigger > current
        else:
            valid = trigger > current if position_side == "LONG" else trigger < current
        if not valid:
            raise ValueError("保护单触发价与当前行情及仓位方向不匹配")

        order = PaperOrder(
            order_id=f"PC-{uuid4().hex}",
            client_order_id=client_order_id,
            symbol=symbol,
            order_type=order_type,
            side=side,
            position_side=position_side,
            quantity=requested,
            trigger_price=trigger,
        )
        db.session.add(order)
        db.session.commit()
        return self._paper_order_result(order)

    @staticmethod
    def _paper_order_result(order: PaperOrder) -> Dict:
        return {
            "id": order.order_id,
            "clientOrderId": order.client_order_id,
            "symbol": order.symbol,
            "type": order.order_type,
            "side": order.side.lower(),
            "positionSide": order.position_side,
            "amount": float(order.quantity),
            "stopPrice": float(order.trigger_price),
            "status": order.status.lower(),
            "paper": True,
        }

    def create_stop_loss_order(
        self, symbol, side, quantity, stop_price, position_side, client_order_id=None
    ) -> Dict:
        return self._create_conditional_order(
            "STOP_MARKET",
            symbol,
            side,
            quantity,
            stop_price,
            position_side,
            client_order_id,
        )

    def create_take_profit_order(
        self,
        symbol,
        side,
        quantity,
        take_profit_price,
        position_side,
        client_order_id=None,
    ) -> Dict:
        return self._create_conditional_order(
            "TAKE_PROFIT_MARKET",
            symbol,
            side,
            quantity,
            take_profit_price,
            position_side,
            client_order_id,
        )

    def get_open_orders(self, symbol: str = None) -> List[Dict]:
        query = PaperOrder.query.filter_by(status="NEW")
        if symbol:
            query = query.filter_by(symbol=symbol)
        return [self._paper_order_result(order) for order in query.all()]

    def cancel_all_orders(self, symbol: str) -> List[Dict]:
        with self._lock:
            try:
                orders = PaperOrder.query.filter_by(symbol=symbol, status="NEW").all()
                results = [self._paper_order_result(order) for order in orders]
                for order in orders:
                    order.status = "CANCELLED"
                db.session.commit()
                return results
            except Exception:
                db.session.rollback()
                raise

    def cancel_position_orders(self, symbol: str, position_side: str) -> List[Dict]:
        with self._lock:
            try:
                orders = PaperOrder.query.filter_by(
                    symbol=symbol,
                    position_side=position_side.upper(),
                    status="NEW",
                ).all()
                results = [self._paper_order_result(order) for order in orders]
                for order in orders:
                    order.status = "CANCELLED"
                db.session.commit()
                return results
            except Exception:
                db.session.rollback()
                raise

    def cancel_orders_by_type(self, symbol: str, order_type: str) -> List[Dict]:
        with self._lock:
            try:
                if order_type == "all":
                    return self.cancel_all_orders(symbol)
                exchange_type = {
                    "stop_loss": "STOP_MARKET",
                    "take_profit": "TAKE_PROFIT_MARKET",
                }.get(order_type)
                if exchange_type is None:
                    raise ValueError(f"不支持的订单类型: {order_type}")
                orders = PaperOrder.query.filter_by(
                    symbol=symbol, status="NEW", order_type=exchange_type
                ).all()
                results = [self._paper_order_result(order) for order in orders]
                for order in orders:
                    order.status = "CANCELLED"
                db.session.commit()
                return results
            except Exception:
                db.session.rollback()
                raise

    def cancel_order_by_id(self, symbol: str, order_id: str) -> Dict:
        with self._lock:
            try:
                order = PaperOrder.query.filter_by(
                    symbol=symbol, order_id=order_id, status="NEW"
                ).first()
                if order is None:
                    return {
                        "success": False,
                        "order_id": order_id,
                        "error": "模拟订单不存在",
                    }
                order.status = "CANCELLED"
                db.session.commit()
                return {"success": True, "order_id": order_id, "type": "paper"}
            except Exception:
                db.session.rollback()
                raise

    def set_leverage(self, symbol: str, leverage: int) -> Dict:
        if not 1 <= leverage <= 125:
            raise ValueError("杠杆必须在 1 到 125 之间")
        position = PaperPosition.query.filter_by(symbol=symbol).first()
        if position is not None:
            raise ValueError("存在持仓时不能修改模拟杠杆")
        setting = self._setting(symbol)
        setting.leverage = leverage
        db.session.commit()
        return {"symbol": symbol, "leverage": leverage, "paper": True}

    def set_margin_mode(self, symbol: str, mode: str) -> Dict:
        mode = mode.lower()
        if mode not in {"cross", "isolated"}:
            raise ValueError("保证金模式只能是 cross 或 isolated")
        position = PaperPosition.query.filter_by(symbol=symbol).first()
        if position is not None:
            raise ValueError("存在持仓时不能修改模拟保证金模式")
        setting = self._setting(symbol)
        setting.margin_mode = mode
        db.session.commit()
        return {"symbol": symbol, "marginMode": mode, "paper": True}

    def _resize_protective_orders(
        self,
        symbol: str,
        position_side: str,
        remaining: Decimal,
        excluded_order_id: Optional[str] = None,
    ) -> None:
        orders = PaperOrder.query.filter_by(
            symbol=symbol, position_side=position_side, status="NEW"
        ).all()
        for order in orders:
            if order.order_id == excluded_order_id:
                continue
            if remaining <= 0:
                order.status = "CANCELLED"
            elif _decimal(order.quantity) > remaining:
                order.quantity = remaining

    def process_pending_orders(self, symbols: List[str] = None) -> List[Dict]:
        """用真实最新价触发模拟止盈止损订单。"""
        with self._lock:
            try:
                return self._process_pending_orders_locked(symbols)
            except Exception:
                db.session.rollback()
                raise

    def _process_pending_orders_locked(self, symbols: List[str] = None) -> List[Dict]:
        query = PaperOrder.query.filter_by(status="NEW")
        if symbols:
            query = query.filter(PaperOrder.symbol.in_(symbols))
        orders = query.order_by(PaperOrder.created_at).all()
        prices = {}
        triggered = []
        for order in orders:
            if order.status != "NEW":
                continue
            trigger_client_id = f"trigger-{order.order_id}"
            existing_execution = PaperExecution.query.filter_by(
                client_order_id=trigger_client_id
            ).first()
            if existing_execution is not None:
                order.status = "FILLED"
                order.filled_at = order.filled_at or existing_execution.created_at
                triggered.append(self._execution_result(existing_execution))
                continue
            position = PaperPosition.query.filter_by(
                symbol=order.symbol, side=order.position_side
            ).first()
            if position is None or _decimal(position.quantity) <= 0:
                order.status = "EXPIRED"
                continue
            if order.symbol not in prices:
                prices[order.symbol] = self._price(order.symbol)
            price = prices[order.symbol]
            trigger = _decimal(order.trigger_price)
            if order.order_type == "STOP_MARKET":
                should_trigger = (
                    price <= trigger if order.position_side == "LONG" else price >= trigger
                )
            else:
                should_trigger = (
                    price >= trigger if order.position_side == "LONG" else price <= trigger
                )
            if not should_trigger:
                continue
            result = self.create_market_order(
                order.symbol,
                order.side,
                float(order.quantity),
                order.position_side,
                client_order_id=trigger_client_id,
                source_order_id=order.order_id,
            )
            refreshed = PaperOrder.query.filter_by(order_id=order.order_id).first()
            refreshed.status = "FILLED"
            refreshed.filled_at = utc_now()
            db.session.commit()
            triggered.append(result)
        if orders:
            db.session.commit()
        return triggered
