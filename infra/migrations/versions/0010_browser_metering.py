"""Persist per-session browser counters without request bodies or URLs."""

from alembic import op

revision = "0010_browser_metering"
down_revision = "0009_recovery_observations"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(
        "ALTER TABLE lead.browser_sessions ADD COLUMN usage jsonb NOT NULL DEFAULT '{}'"
    )


def downgrade():
    raise RuntimeError("Usage history cannot be downgraded")
