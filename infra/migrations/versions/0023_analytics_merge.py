"""Join analytics and the owner's existing browser-pilot migration history."""

revision = "0023_analytics_merge"
down_revision = ("0022_analytics_hardening", "0020_owner_browser_pilot")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
