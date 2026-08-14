"""建立旧版数据库基线。"""

from alembic import op
import sqlalchemy as sa


revision = "20260811_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "memory_board",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("last_updated", sa.DateTime()),
    )
    op.create_table(
        "system_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("custom_instructions", sa.Text(), nullable=False),
        sa.Column("last_updated", sa.DateTime()),
    )
    op.create_table(
        "market_snapshot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("timestamp", sa.DateTime()),
        sa.Column("advance_decline_ratio", sa.Float()),
        sa.Column("btc_dominance", sa.Float()),
        sa.Column("indicators_data", sa.Text()),
    )
    op.create_index("ix_market_snapshot_timestamp", "market_snapshot", ["timestamp"])
    op.create_table(
        "trade_decision",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("timestamp", sa.DateTime()),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("display_info", sa.String(255)),
        sa.Column("tool_name", sa.String(50)),
        sa.Column("tool_args", sa.Text()),
        sa.Column("ai_reasoning", sa.Text()),
        sa.Column("snapshot_id", sa.Integer(), sa.ForeignKey("market_snapshot.id")),
        sa.Column("order_id", sa.String(50)),
        sa.Column("executed_price", sa.Float()),
        sa.Column("executed_quantity", sa.Float()),
        sa.Column("execution_status", sa.String(20)),
    )
    op.create_index("ix_trade_decision_timestamp", "trade_decision", ["timestamp"])
    op.create_table(
        "equity_snapshot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("timestamp", sa.DateTime()),
        sa.Column("total_equity", sa.Float(), nullable=False),
        sa.Column("free_balance", sa.Float(), nullable=False),
        sa.Column("unrealized_pnl", sa.Float()),
        sa.Column("position_count", sa.Integer()),
    )
    op.create_index("ix_equity_snapshot_timestamp", "equity_snapshot", ["timestamp"])
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


def downgrade():
    op.drop_table("pending_order")
    op.drop_table("equity_snapshot")
    op.drop_table("trade_decision")
    op.drop_table("market_snapshot")
    op.drop_table("system_settings")
    op.drop_table("memory_board")
