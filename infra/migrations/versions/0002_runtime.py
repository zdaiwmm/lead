"""Freeze the previously validated runtime schema as an explicit revision."""

from pathlib import Path

from alembic import op

revision = "0002_runtime"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(
        (Path(__file__).parent.parent / "sql/0002_runtime.sql").read_text(),
        execution_options={"no_parameters": True},
    )


def downgrade():
    raise RuntimeError("Destructive schema downgrade is disabled")
