"""Workspace-managed model choices share server-side provider credentials."""
from alembic import op

revision = "0015_admin_model_catalog"
down_revision = "0014_admin_governance"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql("ALTER TABLE lead.admin_configs DROP CONSTRAINT admin_configs_kind_check; ALTER TABLE lead.admin_configs ADD CHECK(kind IN ('budget','strategy','onboarding','models','brand','runtime','notifications','channel_rule','attribution','experience','model_catalog'))",execution_options={"no_parameters":True})


def downgrade():
    raise RuntimeError("Published model catalogs and history must be retained")
