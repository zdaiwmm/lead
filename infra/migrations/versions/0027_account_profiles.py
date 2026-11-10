"""Keep the original project profile and permit isolated login slots."""

from alembic import op

revision = "0027_account_profiles"
down_revision = "0026_first_entry"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE lead.social_accounts ADD COLUMN binding_version bigint NOT NULL DEFAULT 1;
        ALTER TABLE lead.browser_profiles ADD COLUMN slot text NOT NULL DEFAULT 'default';
        ALTER TABLE lead.browser_profiles DROP CONSTRAINT browser_profiles_tenant_id_agent_id_key;
        ALTER TABLE lead.browser_profiles ADD UNIQUE (tenant_id,agent_id,slot);
    """)


def downgrade():
    raise RuntimeError(
        "Account profiles must be drained and reconciled before downgrade"
    )
