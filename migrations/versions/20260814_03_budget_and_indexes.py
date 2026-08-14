"""记录周期 token 用量、补齐热查询索引并清理遗留表。"""

from alembic import op
import sqlalchemy as sa


revision = "20260814_03"
down_revision = "20260811_02"
branch_labels = None
depends_on = None


def upgrade():
    # 周期级 token 用量是模型预算熔断的依据
    with op.batch_alter_table("trading_cycle") as batch:
        batch.add_column(
            sa.Column(
                "tokens_used", sa.Integer(), nullable=False, server_default="0"
            )
        )
        batch.create_index("ix_trading_cycle_started_at", ["started_at"])

    # 每个周期都会按 execution_status 探测待对账记录
    op.create_index(
        "ix_trade_decision_execution_status",
        "trade_decision",
        ["execution_status"],
    )
    # 净值熔断按交易模式过滤并按时间排序
    op.create_index(
        "ix_equity_snapshot_mode_timestamp",
        "equity_snapshot",
        ["trading_mode", "timestamp"],
    )

    # 模拟钱包固定单行，用约束固化该不变量
    with op.batch_alter_table("paper_account") as batch:
        batch.create_check_constraint("ck_paper_account_single_row", "id = 1")

    # btc_dominance 自建表起从未被写入
    with op.batch_alter_table("market_snapshot") as batch:
        batch.drop_column("btc_dominance")

    # pending_order 自模拟账本引入后已无任何读写方
    op.drop_index("ix_pending_order_created_at", table_name="pending_order")
    op.drop_index("ix_pending_order_symbol", table_name="pending_order")
    op.drop_table("pending_order")


def downgrade():
    op.create_table(
        "pending_order",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("order_id", sa.String(50), nullable=False),
        sa.Column("order_type", sa.String(30)),
        sa.Column("side", sa.String(10)),
        sa.Column("quantity", sa.Float()),
        sa.Column("trigger_price", sa.Float()),
        sa.Column("is_algo", sa.Boolean()),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("status", sa.String(20)),
    )
    op.create_index("ix_pending_order_symbol", "pending_order", ["symbol"])
    op.create_index("ix_pending_order_created_at", "pending_order", ["created_at"])

    with op.batch_alter_table("market_snapshot") as batch:
        batch.add_column(sa.Column("btc_dominance", sa.Float()))

    with op.batch_alter_table("paper_account") as batch:
        batch.drop_constraint("ck_paper_account_single_row", type_="check")

    op.drop_index(
        "ix_equity_snapshot_mode_timestamp", table_name="equity_snapshot"
    )
    op.drop_index(
        "ix_trade_decision_execution_status", table_name="trade_decision"
    )
    with op.batch_alter_table("trading_cycle") as batch:
        batch.drop_index("ix_trading_cycle_started_at")
        batch.drop_column("tokens_used")
