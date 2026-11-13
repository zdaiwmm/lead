"""Persistent local Codex execution transport, preserving all business identities."""
from pathlib import Path

from alembic import op

revision = "0034_local_executor"
down_revision = "0033_unified_browser"
branch_labels = None
depends_on = None


def upgrade():
    path = Path(__file__).resolve().parents[1] / "sql/0034_local_executor.sql"
    op.get_bind().exec_driver_sql(path.read_text(), execution_options={"no_parameters": True})


def downgrade():
    raise RuntimeError("Local execution and original external-action evidence must be retained")
