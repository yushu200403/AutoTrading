"""
OpenNOF1 的数据库模型。

定义记忆、快照、交易决策和账户净值历史的数据结构。
"""

from datetime import datetime, timedelta, timezone
from app import db


def utc_now():
    """返回带时区的 UTC 时间。"""
    return datetime.now(timezone.utc)


def naive_utc_now():
    """返回不带时区的 UTC 时间。

    早期建立的表使用无时区列存放 UTC 值，读取侧统一按 UTC 解释。
    这里替代已废弃的 datetime.utcnow，保持存储语义不变。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MemoryBoard(db.Model):
    """
    AI 记忆白板 - 单行记录，总是更新。
    
    这是"无限白板"，赋予 AI 跨越交易周期的持续市场认知。
    """
    __tablename__ = 'memory_board'
    
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False, default='')
    last_updated = db.Column(
        db.DateTime, 
        default=naive_utc_now, 
        onupdate=naive_utc_now
    )
    
    @classmethod
    def get_or_create(cls):
        """获取单例记忆白板，如果需要则创建。"""
        board = cls.query.first()
        if board is None:
            board = cls(content='')
            db.session.add(board)
            db.session.commit()
        return board
    
    def update(self, content: str):
        """更新白板内容。"""
        self.content = content
        self.last_updated = naive_utc_now()
        db.session.commit()
    
    def __repr__(self):
        return f'<MemoryBoard updated={self.last_updated}>'


class SystemSettings(db.Model):
    """
    系统设置 - 单行记录，存储持久化配置。
    
    包括自定义交易指令等需要跨重启保留的设置。
    """
    __tablename__ = 'system_settings'
    
    id = db.Column(db.Integer, primary_key=True)
    custom_instructions = db.Column(db.Text, nullable=False, default='')
    last_updated = db.Column(
        db.DateTime, 
        default=naive_utc_now, 
        onupdate=naive_utc_now
    )
    
    @classmethod
    def get_or_create(cls):
        """获取单例设置，如果需要则创建。"""
        settings = cls.query.first()
        if settings is None:
            settings = cls(custom_instructions='')
            db.session.add(settings)
            db.session.commit()
        return settings
    
    def update_instructions(self, instructions: str):
        """更新自定义指令。"""
        self.custom_instructions = instructions
        self.last_updated = naive_utc_now()
        db.session.commit()
    
    def __repr__(self):
        return f'<SystemSettings updated={self.last_updated}>'


class MarketSnapshot(db.Model):
    """
    记录每个 AI 决策点的关键指标。
    
    用于回测和 AI 决策分析。
    """
    __tablename__ = 'market_snapshot'
    
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=naive_utc_now, index=True)
    
    # 市场宽度
    advance_decline_ratio = db.Column(db.Float)
    
    # 所有技术指标 (序列化的 JSON)
    indicators_data = db.Column(db.Text)
    
    # 相关交易决策
    decisions = db.relationship('TradeDecision', backref='snapshot', lazy='dynamic')
    
    def __repr__(self):
        return f'<MarketSnapshot {self.timestamp} A/D={self.advance_decline_ratio}>'


class TradeDecision(db.Model):
    """
    记录 AI 做出的每个交易决策。
    
    存储采取的行动和推理。
    """
    __tablename__ = 'trade_decision'
    
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime(timezone=True), default=utc_now, index=True)
    cycle_id = db.Column(db.String(36), index=True)
    trading_mode = db.Column(db.String(10), nullable=False, default='paper', index=True)
    
    # 交易详情
    symbol = db.Column(db.String(20), nullable=False)
    action = db.Column(db.String(20), nullable=False)  # LONG、SHORT、CLOSE、HOLD、MEMORY 等
    
    # 前端显示信息
    display_info = db.Column(db.String(255))
    
    # 工具调用详情
    tool_name = db.Column(db.String(50))  # update_memory, trade_in, close_position
    tool_args = db.Column(db.Text)  # JSON 格式的参数
    
    # 完整的 AI 推理 (思维链)
    ai_reasoning = db.Column(db.Text)
    
    # 链接到市场快照
    snapshot_id = db.Column(
        db.Integer, 
        db.ForeignKey('market_snapshot.id'),
        nullable=True
    )
    
    # 执行详情
    order_id = db.Column(db.String(50))
    executed_price = db.Column(db.Numeric(28, 12))
    executed_quantity = db.Column(db.Numeric(28, 12))
    # 每个周期都会按该列探测待对账记录，因此建立索引
    execution_status = db.Column(db.String(20), index=True)
    
    def __repr__(self):
        return f'<TradeDecision {self.symbol} {self.action} @ {self.timestamp}>'


class EquitySnapshot(db.Model):
    """
    账户净值快照 - 用于绘制收益曲线。
    
    每次交易循环结束后记录账户状态。
    """
    __tablename__ = 'equity_snapshot'
    __table_args__ = (
        # 收益曲线查询按交易模式过滤并按时间排序
        db.Index('ix_equity_snapshot_mode_timestamp', 'trading_mode', 'timestamp'),
    )
    
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime(timezone=True), default=utc_now, index=True)
    trading_mode = db.Column(db.String(10), nullable=False, default='paper', index=True)
    
    # 账户数据
    total_equity = db.Column(db.Numeric(28, 12), nullable=False)
    free_balance = db.Column(db.Numeric(28, 12), nullable=False)
    unrealized_pnl = db.Column(db.Numeric(28, 12), default=0)
    
    # 持仓数量
    position_count = db.Column(db.Integer, default=0)
    
    @classmethod
    def get_latest(cls, trading_mode: str = None):
        """获取最新的净值快照。"""
        query = cls.query
        if trading_mode:
            query = query.filter_by(trading_mode=trading_mode)
        return query.order_by(cls.timestamp.desc()).first()
    
    @classmethod
    def get_first(cls, trading_mode: str = None):
        """获取最早的净值快照（基准线）。"""
        query = cls.query
        if trading_mode:
            query = query.filter_by(trading_mode=trading_mode)
        return query.order_by(cls.timestamp.asc()).first()
    
    @classmethod
    def get_history(cls, limit: int = None, trading_mode: str = None):
        """获取净值历史记录（按时间升序）。
        
        参数:
            limit: 最大记录数，None 表示不限制
        """
        query = cls.query
        if trading_mode:
            query = query.filter_by(trading_mode=trading_mode)
        query = query.order_by(cls.timestamp.desc())
        if limit is not None:
            query = query.limit(limit)
        records = query.all()
        return records[::-1]
    
    @classmethod
    def get_24h_ago(cls, trading_mode: str = None):
        """获取24小时前的净值快照。"""
        target_time = utc_now() - timedelta(hours=24)
        query = cls.query.filter(cls.timestamp <= target_time)
        if trading_mode:
            query = query.filter_by(trading_mode=trading_mode)
        return query.order_by(cls.timestamp.desc()).first()
    
    def __repr__(self):
        return f'<EquitySnapshot {self.timestamp} equity={self.total_equity}>'


class PaperAccount(db.Model):
    """模拟交易钱包，固定使用 ID=1。"""

    __tablename__ = 'paper_account'
    __table_args__ = (
        db.CheckConstraint('id = 1', name='ck_paper_account_single_row'),
    )

    id = db.Column(db.Integer, primary_key=True, default=1)
    wallet_balance = db.Column(db.Numeric(28, 12), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class PaperPosition(db.Model):
    """模拟双向持仓。"""

    __tablename__ = 'paper_position'
    __table_args__ = (
        db.UniqueConstraint('symbol', 'side', name='uq_paper_position_symbol_side'),
        db.CheckConstraint("side IN ('LONG', 'SHORT')", name='ck_paper_position_side'),
    )

    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(20), nullable=False, index=True)
    side = db.Column(db.String(10), nullable=False)
    quantity = db.Column(db.Numeric(28, 12), nullable=False)
    entry_price = db.Column(db.Numeric(28, 12), nullable=False)
    leverage = db.Column(db.Integer, nullable=False, default=1)
    margin_mode = db.Column(db.String(10), nullable=False, default='cross')
    realized_pnl = db.Column(db.Numeric(28, 12), nullable=False, default=0)
    updated_at = db.Column(
        db.DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class PaperSymbolSetting(db.Model):
    """模拟交易对的杠杆和保证金模式。"""

    __tablename__ = 'paper_symbol_setting'

    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(20), nullable=False, unique=True, index=True)
    leverage = db.Column(db.Integer, nullable=False, default=1)
    margin_mode = db.Column(db.String(10), nullable=False, default='cross')
    updated_at = db.Column(
        db.DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class PaperOrder(db.Model):
    """模拟条件委托。"""

    __tablename__ = 'paper_order'
    __table_args__ = (
        db.CheckConstraint(
            "status IN ('NEW', 'FILLED', 'CANCELLED', 'EXPIRED')",
            name='ck_paper_order_status',
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.String(64), nullable=False, unique=True, index=True)
    client_order_id = db.Column(db.String(64), unique=True, index=True)
    symbol = db.Column(db.String(20), nullable=False, index=True)
    order_type = db.Column(db.String(30), nullable=False)
    side = db.Column(db.String(10), nullable=False)
    position_side = db.Column(db.String(10), nullable=False)
    quantity = db.Column(db.Numeric(28, 12), nullable=False)
    trigger_price = db.Column(db.Numeric(28, 12), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='NEW', index=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now, nullable=False)
    filled_at = db.Column(db.DateTime(timezone=True))


class PaperExecution(db.Model):
    """模拟市价成交与幂等结果。"""

    __tablename__ = 'paper_execution'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.String(64), nullable=False, unique=True, index=True)
    client_order_id = db.Column(db.String(64), nullable=False, unique=True, index=True)
    symbol = db.Column(db.String(20), nullable=False, index=True)
    side = db.Column(db.String(10), nullable=False)
    position_side = db.Column(db.String(10), nullable=False)
    quantity = db.Column(db.Numeric(28, 12), nullable=False)
    executed_price = db.Column(db.Numeric(28, 12), nullable=False)
    fee = db.Column(db.Numeric(28, 12), nullable=False, default=0)
    realized_pnl = db.Column(db.Numeric(28, 12), nullable=False, default=0)
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now, nullable=False)


class TradingCycle(db.Model):
    """一次 AI 决策周期的可追溯状态。"""

    __tablename__ = 'trading_cycle'

    cycle_id = db.Column(db.String(36), primary_key=True)
    trading_mode = db.Column(db.String(10), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default='RUNNING', index=True)
    started_at = db.Column(
        db.DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )
    finished_at = db.Column(db.DateTime(timezone=True))
    tokens_used = db.Column(db.Integer, nullable=False, default=0)
    error = db.Column(db.Text)

