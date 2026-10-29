from alembic import context

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Use scripts/migrate.py with the operator database credentials")
context.configure(
    connection=connection,
    version_table="lead_migration_version",
    transaction_per_migration=True,
)
with context.begin_transaction():
    context.run_migrations()
