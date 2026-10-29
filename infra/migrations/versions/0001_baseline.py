"""Install an empty database, or validate the pre-Alembic lead baseline."""

from pathlib import Path

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    exists = connection.exec_driver_sql("SELECT to_regclass('lead.agents')").scalar()
    if not exists:
        connection.exec_driver_sql(
            (Path(__file__).parent.parent / "sql/0001_baseline.sql").read_text(),
            execution_options={"no_parameters": True},
        )
        return
    required = {
        "agents": {"tenant_id", "config_revision", "control_version"},
        "actions": {"operation_key", "expected_control_version", "intent", "state"},
        "browser_profiles": {"storage_ref", "config_hash", "network_identity_id"},
        "memberships": {"oidc_issuer", "oidc_subject", "role"},
    }
    for table, columns in required.items():
        found = set(
            connection.exec_driver_sql(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='lead' AND table_name=%s",
                (table,),
            ).scalars()
        )
        if not columns <= found:
            raise RuntimeError(
                "Existing lead schema is incompatible; migration stopped"
            )
    if not connection.exec_driver_sql(
        "SELECT relforcerowsecurity FROM pg_class WHERE oid='lead.actions'::regclass"
    ).scalar():
        raise RuntimeError("Existing action ledger does not enforce RLS")


def downgrade():
    raise RuntimeError(
        "Destructive schema downgrade is disabled; restore into a new database"
    )
