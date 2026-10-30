"""Keep account login and autonomous operation as separate states."""

from alembic import op

revision = "0011_channel_controls"
down_revision = "0010_browser_metering"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(
        "ALTER TABLE lead.social_accounts ADD COLUMN automation_enabled boolean NOT NULL DEFAULT true"
    )


def downgrade():
    raise RuntimeError("Channel control history cannot be downgraded")
