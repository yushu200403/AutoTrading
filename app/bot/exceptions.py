"""
OpenNOF1 的自定义异常。

遵循修复法则 (Repair Rule)：当必须失败时，要尽早且大声地失败。
每个异常都清楚地表明失败点和上下文。
"""


class OpenNOF1Error(Exception):
    """OpenNOF1 的基类异常。"""
    pass


class DataFetchError(OpenNOF1Error):
    """当无法从外部源获取数据时引发。"""
    
    def __init__(self, source: str, symbol: str = None, reason: str = None):
        self.source = source
        self.symbol = symbol
        self.reason = reason
        
        msg = f"无法从 {source} 获取数据"
        if symbol:
            msg += f"，交易对 {symbol}"
        if reason:
            msg += f"：{reason}"
        
        super().__init__(msg)


class InsufficientDataError(OpenNOF1Error):
    """当数据不足以进行计算时引发。"""
    
    def __init__(self, symbol: str, required: int, received: int):
        self.symbol = symbol
        self.required = required
        self.received = received
        
        super().__init__(
            f"{symbol} 数据不足：需要 {required} 根 K 线，实际 {received} 根"
        )


class AuthenticationError(OpenNOF1Error):
    """当 API 凭证丢失或无效时引发。"""
    
    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        super().__init__(
            f"访问 {endpoint} 需要 API 凭证，"
            "请在环境变量中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET"
        )


class ConfigurationError(OpenNOF1Error):
    """当配置无效或丢失时引发。"""
    
    def __init__(self, key: str, reason: str = None):
        self.key = key
        msg = f"配置项 {key} 无效"
        if reason:
            msg += f"：{reason}"
        super().__init__(msg)


class OrderExecutionError(OpenNOF1Error):
    """当订单执行失败时引发。"""
    
    def __init__(self, symbol: str, side: str, reason: str):
        self.symbol = symbol
        self.side = side
        self.reason = reason
        super().__init__(
            f"执行 {symbol} {side} 订单失败：{reason}"
        )


class OrderResultUnknownError(OrderExecutionError):
    """请求结果未知，必须先与交易所对账，禁止直接重试。"""

    def __init__(self, symbol: str, side: str, client_order_id: str, reason: str):
        self.client_order_id = client_order_id
        super().__init__(
            symbol,
            side,
            f"订单结果未知，客户端订单 ID={client_order_id}：{reason}",
        )


class ReconciliationRequiredError(OpenNOF1Error):
    """存在待对账的交易意图，必须人工核对交易执行端后才能继续交易。"""

    def __init__(self, decision_id: int, status: str, trading_mode: str):
        self.decision_id = decision_id
        self.status = status
        self.trading_mode = trading_mode
        super().__init__(
            f"{trading_mode} 模式存在待对账交易意图 {decision_id}（状态 {status}），"
            "必须人工核对交易执行端后才能继续交易"
        )


class RiskLimitBreachedError(OpenNOF1Error):
    """账户级风险熔断已触发，禁止继续开仓。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"账户级风险熔断已触发：{reason}")


class InsufficientBalanceError(OpenNOF1Error):
    """当余额不足以进行交易时引发。"""
    
    def __init__(self, required: float, available: float):
        self.required = required
        self.available = available
        super().__init__(
            f"余额不足：需要 {required:.2f} USDT，"
            f"可用 {available:.2f} USDT"
        )


class PositionNotFoundError(OpenNOF1Error):
    """当尝试平仓不存在的仓位时引发。"""
    
    def __init__(self, symbol: str):
        self.symbol = symbol
        super().__init__(f"未找到 {symbol} 的可用仓位")
