"""Owner-only versioned schema upgrades; never a worker startup action."""

import psycopg
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from .config import ROOT, database_kwargs
from .provision import provision


def upgrade(kwargs=None, target="head", roles=True):
    engine = create_engine(
        "postgresql+psycopg://",
        creator=lambda: psycopg.connect(**(kwargs or database_kwargs(True))),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(735847201)")
            cfg = Config(str(ROOT / "infra/alembic.ini"))
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, target)
            if roles:
                provision(connection.connection.driver_connection)
    finally:
        engine.dispose()
