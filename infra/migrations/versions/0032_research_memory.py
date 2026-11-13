"""Scoped research allowances and evidence-backed experience projections."""
from pathlib import Path

from alembic import op

revision = '0032_research_memory'
down_revision = '0031_growth_runtime'
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql((Path(__file__).resolve().parents[1] / 'sql/0032_research_memory.sql').read_text(), execution_options={'no_parameters': True})


def downgrade():
    raise RuntimeError('Research billing and evidence history must be retained')
