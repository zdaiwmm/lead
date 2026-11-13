"""Version original connection control and native request claim identity."""
from pathlib import Path

from alembic import op

revision = "0033_unified_browser"
down_revision = "0032_research_memory"
branch_labels = None
depends_on = None


def upgrade():
    path = Path(__file__).resolve().parents[1] / "sql/0033_unified_browser.sql"
    op.get_bind().exec_driver_sql(path.read_text(), execution_options={"no_parameters": True})


def downgrade():
    raise RuntimeError("Original connection and external-action evidence must be retained")
