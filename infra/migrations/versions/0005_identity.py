"""OIDC identities, opaque sessions and restricted identity bootstrap functions."""

from pathlib import Path

from alembic import op

revision = "0005_identity"
down_revision = "0004_profile_maintenance"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(
        (Path(__file__).parent.parent / "sql/0005_identity.sql").read_text(),
        execution_options={"no_parameters": True},
    )


def downgrade():
    raise RuntimeError("Identity/session downgrade is disabled")
