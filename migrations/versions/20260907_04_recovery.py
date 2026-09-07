"""持久化订单身份、失败原因和自动修复进度。"""

from alembic import op
import sqlalchemy as sa


revision = "20260907_04"
down_revision = "20260814_03"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("trade_decision") as batch:
        batch.add_column(sa.Column("client_order_id", sa.String(36)))
        batch.add_column(sa.Column("execution_error", sa.Text()))
        batch.add_column(sa.Column("recovery_state", sa.Text()))


def downgrade():
    with op.batch_alter_table("trade_decision") as batch:
        batch.drop_column("recovery_state")
        batch.drop_column("execution_error")
        batch.drop_column("client_order_id")
