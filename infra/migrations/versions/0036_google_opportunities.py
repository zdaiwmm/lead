"""Keep Google login policy and site attempts scoped to the original Agent."""

from pathlib import Path

from alembic import op

revision = "0036_google_opportunities"
down_revision = "0035_shared_executor"
branch_labels = None
depends_on = None


def upgrade():
    sql = Path(__file__).resolve().parents[1] / "sql/0036_google_opportunities.sql"
    op.get_bind().exec_driver_sql(
        sql.read_text(), execution_options={"no_parameters": True}
    )


def downgrade():
    op.drop_table("google_opportunity_attempts", schema="lead")
    op.drop_table("google_opportunity_policies", schema="lead")
