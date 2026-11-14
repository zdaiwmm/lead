"""Managed compute identity and tenant-owned studio work, without v1 migration."""
from pathlib import Path

from alembic import op

revision = '0035_shared_executor'
down_revision = '0034_local_executor'
branch_labels = None
depends_on = None


def upgrade():
    sql = Path(__file__).resolve().parents[1] / 'sql/0035_shared_executor.sql'
    op.get_bind().exec_driver_sql(sql.read_text(), execution_options={'no_parameters': True})


def downgrade():
    raise RuntimeError('Execution identities and original request evidence must be retained')
