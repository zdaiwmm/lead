"""Keep arrival evidence separate from order attribution and inferred identities."""

from alembic import op

revision = "0026_first_entry"
down_revision = "0025_site_devices"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE lead.pixel_receipts
          ADD COLUMN entry_link_id uuid,
          ADD COLUMN server_identified boolean NOT NULL DEFAULT false,
          ADD FOREIGN KEY (tenant_id,agent_id,entry_link_id)
            REFERENCES lead.tracking_links(tenant_id,agent_id,id);
        CREATE INDEX pixel_first_landing ON lead.pixel_receipts
          (connection_id,anonymous_id,occurred_at,event_id)
          WHERE scope_key='live' AND counted AND event_name='PageView';
        CREATE INDEX pixel_customer_evidence ON lead.pixel_receipts
          (connection_id,user_key,event_name,occurred_at)
          WHERE scope_key='live' AND server_identified;
        CREATE INDEX pixel_device_customers ON lead.pixel_receipts
          (connection_id,anonymous_id,user_key)
          WHERE scope_key='live' AND server_identified;
    """)


def downgrade():
    raise RuntimeError("First-entry evidence must be retained")
