"""Alembic 迁移环境。"""

from logging.config import fileConfig

from alembic import context
from flask import current_app


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def get_db():
    return current_app.extensions["migrate"].db


def get_url():
    return str(get_db().engine.url).replace("%", "%%")


config.set_main_option("sqlalchemy.url", get_url())
target_metadata = get_db().metadata


def run_migrations_offline():
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    configure_args = dict(current_app.extensions["migrate"].configure_args)
    with get_db().engine.connect() as connection:
        configure_args.setdefault(
            "render_as_batch", connection.dialect.name == "sqlite"
        )
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            **configure_args,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
