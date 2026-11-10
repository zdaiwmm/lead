"""Record the original account and placement of a distributed link."""

from alembic import op

revision = "0028_distribution_slots"
down_revision = "0027_account_profiles"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE lead.tracking_links ADD COLUMN intent_key text, ADD COLUMN placement text,
            ADD COLUMN account_id uuid, ADD COLUMN placement_ref text, ADD COLUMN campaign text,
            ADD UNIQUE(tenant_id,agent_id,platform,intent_key,placement),
            ADD FOREIGN KEY(tenant_id,agent_id,account_id) REFERENCES lead.social_accounts(tenant_id,agent_id,id);
        CREATE FUNCTION lead.keep_tracking_source() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF (to_jsonb(NEW)-'state') IS DISTINCT FROM (to_jsonb(OLD)-'state') THEN
                RAISE EXCEPTION 'tracking source is immutable';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER keep_tracking_source BEFORE UPDATE ON lead.tracking_links
            FOR EACH ROW EXECUTE FUNCTION lead.keep_tracking_source();
    """)


def downgrade():
    raise RuntimeError("Retain assigned distribution identities")
