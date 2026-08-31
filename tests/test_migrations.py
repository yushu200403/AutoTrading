"""数据库迁移链回归测试。"""

from pathlib import Path

import sqlalchemy as sa
from flask_migrate import upgrade

from app import create_app, db
from tests.conftest import TestConfig


MIGRATIONS_DIRECTORY = Path(__file__).resolve().parents[1] / "migrations"


def _migration_app(database_path):
    database_uri = f"sqlite:///{database_path.as_posix()}"
    config = type(
        "迁移测试配置",
        (TestConfig,),
        {
            "AUTO_CREATE_SCHEMA": False,
            "SQLALCHEMY_DATABASE_URI": database_uri,
            "SQLALCHEMY_ENGINE_OPTIONS": {},
        },
    )
    return create_app(config)


def test_empty_database_upgrades_to_head(tmp_path):
    application = _migration_app(tmp_path / "empty.db")

    with application.app_context():
        upgrade(directory=str(MIGRATIONS_DIRECTORY))
        inspector = sa.inspect(db.engine)
        tables = set(inspector.get_table_names())
        cycle_columns = {c["name"] for c in inspector.get_columns("trading_cycle")}
        decision_indexes = {
            index["name"] for index in inspector.get_indexes("trade_decision")
        }
        revision = db.session.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()

    assert {
        "paper_account",
        "paper_position",
        "paper_order",
        "paper_execution",
        "trading_cycle",
    }.issubset(tables)
    # 遗留的挂单表已随模拟账本落地而移除
    assert "pending_order" not in tables
    # token 审计与待对账探测所需的结构
    assert "tokens_used" in cycle_columns
    assert "ix_trade_decision_execution_status" in decision_indexes
    assert revision == "20260814_03"


def test_legacy_records_are_backfilled_as_live(tmp_path):
    application = _migration_app(tmp_path / "legacy.db")

    with application.app_context():
        upgrade(revision="20260811_01", directory=str(MIGRATIONS_DIRECTORY))
        db.session.execute(
            sa.text(
                "INSERT INTO trade_decision "
                "(symbol, action, execution_status) "
                "VALUES ('BTC/USDT', 'LONG', 'SUCCESS')"
            )
        )
        db.session.execute(
            sa.text(
                "INSERT INTO equity_snapshot "
                "(total_equity, free_balance, unrealized_pnl, position_count) "
                "VALUES (1000, 900, 100, 1)"
            )
        )
        db.session.commit()

        upgrade(directory=str(MIGRATIONS_DIRECTORY))

        decision_mode = db.session.execute(
            sa.text("SELECT trading_mode FROM trade_decision")
        ).scalar_one()
        equity_mode = db.session.execute(
            sa.text("SELECT trading_mode FROM equity_snapshot")
        ).scalar_one()

    assert decision_mode == "live"
    assert equity_mode == "live"
