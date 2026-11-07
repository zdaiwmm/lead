"""Rebuild existing daily PageView device sets for the acquisition overview."""

from alembic import op

revision = "0025_site_devices"
down_revision = "0024_analytics_geography"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        INSERT INTO lead.analytics_report_dirty(tenant_id,agent_id,scope_key,day)
        SELECT DISTINCT tenant_id,agent_id,scope_key,day FROM lead.analytics_rollups
        WHERE grain='day' AND scope_key='live' AND pv>0
        ON CONFLICT DO NOTHING
    """)


def downgrade():
    pass
