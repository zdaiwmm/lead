"""Canonical experiments, account intentions and execution routes."""
from pathlib import Path

from alembic import op

revision = "0031_growth_runtime"
down_revision = "0030_fault_isolation"
branch_labels = None
depends_on = None


def upgrade():
    path = Path(__file__).resolve().parents[1] / "sql/0031_growth_runtime.sql"
    op.get_bind().exec_driver_sql(path.read_text(), execution_options={"no_parameters": True})


def downgrade():
    raise RuntimeError("Growth and external operation history must be retained")
