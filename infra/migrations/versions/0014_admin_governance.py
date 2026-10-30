"""Channel restrictions, attribution snapshots and reviewed strategy experience."""

from alembic import op

revision = "0014_admin_governance"
down_revision = "0013_admin_operations"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(
        """
      ALTER TABLE lead.admin_configs DROP CONSTRAINT admin_configs_kind_check;
      ALTER TABLE lead.admin_configs ADD CHECK(kind IN ('budget','strategy','onboarding','models','brand','runtime','notifications','channel_rule','attribution','experience'));
      ALTER TABLE lead.visit_sessions ADD COLUMN attribution_policy jsonb NOT NULL DEFAULT '{}';
      ALTER TABLE lead.visit_sessions ADD COLUMN attribution_version text NOT NULL DEFAULT 'attribution/builtin@0';
      ALTER TABLE lead.conversion_events ADD COLUMN attribution_version text NOT NULL DEFAULT 'attribution/builtin@0';
      ALTER TABLE lead.budget_reservations ADD COLUMN model_ref text;
      ALTER TABLE lead.budget_reservations ADD COLUMN provider text;
      CREATE INDEX admin_configs_kind_scope ON lead.admin_configs(tenant_id,kind,agent_id);
    """,
        execution_options={"no_parameters": True},
    )


def downgrade():
    raise RuntimeError(
        "Attribution snapshots and configuration history must be retained"
    )
