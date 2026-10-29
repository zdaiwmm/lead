"""Versioned upgrades; runtime never owns or migrates the schema."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
import psycopg
from alembic import command
from alembic.config import Config
from lead_app.config import ROOT, database_kwargs
from lead_app.provision import provision
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool


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


if __name__ == "__main__":
    upgrade()
    print("Versioned database upgrades and scoped runtime grants ready.")
