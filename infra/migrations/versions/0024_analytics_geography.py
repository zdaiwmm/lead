"""Offline country evidence and Agent-scoped materialized geography counts."""

from alembic import op

revision = "0024_analytics_geography"
down_revision = "0023_analytics_merge"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(
        """
      ALTER TABLE lead.analytics_clicks ADD COLUMN country text,ADD COLUMN geo jsonb NOT NULL DEFAULT '{}';
      CREATE TABLE lead.analytics_geo_rollups(
        tenant_id uuid NOT NULL,agent_id uuid NOT NULL,scope_key text NOT NULL,day date NOT NULL,
        channel text NOT NULL,event_name text NOT NULL,country text NOT NULL CHECK(country ~ '^[A-Z]{2}$'),
        clicks bigint NOT NULL DEFAULT 0,pv bigint NOT NULL DEFAULT 0,server_conversions bigint NOT NULL DEFAULT 0,
        PRIMARY KEY(tenant_id,agent_id,scope_key,day,channel,event_name,country),
        FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
      );
      ALTER TABLE lead.analytics_geo_rollups ENABLE ROW LEVEL SECURITY;
      ALTER TABLE lead.analytics_geo_rollups FORCE ROW LEVEL SECURITY;
      CREATE POLICY analytics_scope ON lead.analytics_geo_rollups
        USING(lead.analytics_scope_matches(tenant_id,agent_id,scope_key))
        WITH CHECK(lead.analytics_scope_matches(tenant_id,agent_id,scope_key));
      CREATE POLICY member_agent_scope ON lead.analytics_geo_rollups AS RESTRICTIVE
        USING(lead.member_agent_allowed(agent_id)) WITH CHECK(lead.member_agent_allowed(agent_id));
      INSERT INTO lead.analytics_report_dirty(tenant_id,agent_id,scope_key,day)
        SELECT tenant_id,agent_id,scope_key,day FROM lead.analytics_report_days ON CONFLICT DO NOTHING;
    """,
        execution_options={"no_parameters": True},
    )


def downgrade():
    raise RuntimeError("Preserve historical geographic evidence")
