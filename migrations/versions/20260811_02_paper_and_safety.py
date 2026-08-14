"""增加模拟交易账本和交易审计字段。"""

from alembic import op
import sqlalchemy as sa


revision = "20260811_02"
down_revision = "20260811_01"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("trade_decision") as batch:
        batch.add_column(sa.Column("cycle_id", sa.String(36)))
        batch.add_column(sa.Column("trading_mode", sa.String(10), nullable=True))
        batch.alter_column("executed_price", type_=sa.Numeric(28, 12))
        batch.alter_column("executed_quantity", type_=sa.Numeric(28, 12))
        batch.create_index("ix_trade_decision_cycle_id", ["cycle_id"])
        batch.create_index("ix_trade_decision_trading_mode", ["trading_mode"])
    op.execute(
        sa.text("UPDATE trade_decision SET trading_mode = 'live' WHERE trading_mode IS NULL")
    )
    with op.batch_alter_table("trade_decision") as batch:
        batch.alter_column(
            "trading_mode",
            existing_type=sa.String(10),
            nullable=False,
        )

    with op.batch_alter_table("equity_snapshot") as batch:
        batch.add_column(sa.Column("trading_mode", sa.String(10), nullable=True))
        batch.alter_column("total_equity", type_=sa.Numeric(28, 12))
        batch.alter_column("free_balance", type_=sa.Numeric(28, 12))
        batch.alter_column("unrealized_pnl", type_=sa.Numeric(28, 12))
        batch.create_index("ix_equity_snapshot_trading_mode", ["trading_mode"])
    op.execute(
        sa.text("UPDATE equity_snapshot SET trading_mode = 'live' WHERE trading_mode IS NULL")
    )
    with op.batch_alter_table("equity_snapshot") as batch:
        batch.alter_column(
            "trading_mode",
            existing_type=sa.String(10),
            nullable=False,
        )

    op.create_table(
        "paper_account",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("wallet_balance", sa.Numeric(28, 12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "paper_position",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("quantity", sa.Numeric(28, 12), nullable=False),
        sa.Column("entry_price", sa.Numeric(28, 12), nullable=False),
        sa.Column("leverage", sa.Integer(), nullable=False),
        sa.Column("margin_mode", sa.String(10), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(28, 12), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("symbol", "side", name="uq_paper_position_symbol_side"),
        sa.CheckConstraint("side IN ('LONG', 'SHORT')", name="ck_paper_position_side"),
    )
    op.create_index("ix_paper_position_symbol", "paper_position", ["symbol"])
    op.create_table(
        "paper_symbol_setting",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(20), nullable=False, unique=True),
        sa.Column("leverage", sa.Integer(), nullable=False),
        sa.Column("margin_mode", sa.String(10), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_paper_symbol_setting_symbol", "paper_symbol_setting", ["symbol"])
    op.create_table(
        "paper_order",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.String(64), nullable=False, unique=True),
        sa.Column("client_order_id", sa.String(64), unique=True),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("order_type", sa.String(30), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("position_side", sa.String(10), nullable=False),
        sa.Column("quantity", sa.Numeric(28, 12), nullable=False),
        sa.Column("trigger_price", sa.Numeric(28, 12), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('NEW', 'FILLED', 'CANCELLED', 'EXPIRED')",
            name="ck_paper_order_status",
        ),
    )
    for column in ("order_id", "client_order_id", "symbol", "status"):
        op.create_index(f"ix_paper_order_{column}", "paper_order", [column])
    op.create_table(
        "paper_execution",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.String(64), nullable=False, unique=True),
        sa.Column("client_order_id", sa.String(64), nullable=False, unique=True),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("position_side", sa.String(10), nullable=False),
        sa.Column("quantity", sa.Numeric(28, 12), nullable=False),
        sa.Column("executed_price", sa.Numeric(28, 12), nullable=False),
        sa.Column("fee", sa.Numeric(28, 12), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(28, 12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("order_id", "client_order_id", "symbol"):
        op.create_index(f"ix_paper_execution_{column}", "paper_execution", [column])
    op.create_table(
        "trading_cycle",
        sa.Column("cycle_id", sa.String(36), primary_key=True),
        sa.Column("trading_mode", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
    )
    op.create_index("ix_trading_cycle_trading_mode", "trading_cycle", ["trading_mode"])
    op.create_index("ix_trading_cycle_status", "trading_cycle", ["status"])


def downgrade():
    op.drop_table("trading_cycle")
    op.drop_table("paper_execution")
    op.drop_table("paper_order")
    op.drop_table("paper_symbol_setting")
    op.drop_table("paper_position")
    op.drop_table("paper_account")
    with op.batch_alter_table("equity_snapshot") as batch:
        batch.drop_index("ix_equity_snapshot_trading_mode")
        batch.alter_column("unrealized_pnl", type_=sa.Float())
        batch.alter_column("free_balance", type_=sa.Float())
        batch.alter_column("total_equity", type_=sa.Float())
        batch.drop_column("trading_mode")
    with op.batch_alter_table("trade_decision") as batch:
        batch.drop_index("ix_trade_decision_trading_mode")
        batch.drop_index("ix_trade_decision_cycle_id")
        batch.alter_column("executed_quantity", type_=sa.Float())
        batch.alter_column("executed_price", type_=sa.Float())
        batch.drop_column("trading_mode")
        batch.drop_column("cycle_id")
