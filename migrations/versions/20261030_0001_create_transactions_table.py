from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transactions",
        sa.Column("transaction_id", sa.String(length=255), nullable=False),
        sa.Column("source_account", sa.String(length=255), nullable=False),
        sa.Column("destination_account", sa.String(length=255), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="PROCESSING", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PROCESSING', 'PROCESSED', 'FAILED')",
            name=op.f("ck_transactions_status_valid"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_transactions_amount_positive")),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_transactions_attempts_non_negative")),
        sa.PrimaryKeyConstraint("transaction_id", name=op.f("pk_transactions")),
    )
    op.create_index(
        "ix_transactions_due",
        "transactions",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_transactions_due",
        table_name="transactions",
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )
    op.drop_table("transactions")
