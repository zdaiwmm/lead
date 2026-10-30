import uuid

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from lead_app.config import ROOT, TENANT, database_kwargs
from psycopg import sql

from scripts.migrate import upgrade


class DatabaseTarget(dict):
    def __repr__(self):
        return f"<isolated database {self['dbname']}>"


@pytest.fixture
def isolated_database():
    name = "lead_test_" + uuid.uuid4().hex
    kwargs = database_kwargs(True)
    with psycopg.connect(**kwargs, autocommit=True) as c:
        c.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    try:
        yield DatabaseTarget({**kwargs, "dbname": name})
    finally:
        with psycopg.connect(**kwargs, autocommit=True) as c:
            c.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


def test_fresh_upgrade_and_repeat_keep_data_and_runtime_permissions(isolated_database):
    upgrade(isolated_database)
    with psycopg.connect(**isolated_database) as c:
        assert (
            c.execute("SELECT version_num FROM lead_migration_version").fetchone()[0]
            == ScriptDirectory.from_config(
                Config(str(ROOT / "infra/alembic.ini"))
            ).get_current_head()
        )
        c.execute(
            "UPDATE lead.tenants SET name='migration-preserved' WHERE id=%s", (TENANT,)
        )
    upgrade(isolated_database)
    with psycopg.connect(**isolated_database) as c:
        assert (
            c.execute(
                "SELECT name FROM lead.tenants WHERE id=%s", (TENANT,)
            ).fetchone()[0]
            == "migration-preserved"
        )
    runtime = {**database_kwargs(), "dbname": isolated_database["dbname"]}
    with psycopg.connect(**runtime) as c:
        assert (
            c.execute("SELECT state FROM lead.recovery_control").fetchone()[0]
            == "normal"
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE lead.recovery_control SET state='normal'")


def test_incremental_upgrade_and_existing_baseline_adoption(isolated_database):
    upgrade(isolated_database, target="0001_baseline", roles=False)
    with psycopg.connect(**isolated_database) as c:
        c.execute("INSERT INTO lead.tenants(id,name) VALUES(%s,'existing')", (TENANT,))
        # Only this newly created test database simulates the previous installer.
        c.execute("DROP TABLE lead_migration_version")
    upgrade(isolated_database)
    with psycopg.connect(**isolated_database) as c:
        assert (
            c.execute(
                "SELECT name FROM lead.tenants WHERE id=%s", (TENANT,)
            ).fetchone()[0]
            == "existing"
        )
        assert (
            c.execute("SELECT count(*) FROM lead.platform_capabilities").fetchone()[0]
            >= 12
        )
