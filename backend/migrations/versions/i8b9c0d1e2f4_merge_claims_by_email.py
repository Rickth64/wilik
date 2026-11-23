"""Add claim quantities and merge claims for the same gift and email address

Revision ID: i8b9c0d1e2f4
Revises: g7b8c9d0e1f2
Create Date: 2026-11-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "i8b9c0d1e2f4"
down_revision = "g7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("claim", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("claimed_quantity", sa.Integer(), nullable=False, server_default="1")
        )
    with op.batch_alter_table("claim", schema=None) as batch_op:
        batch_op.alter_column("claimed_quantity", server_default=None)

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, gift_id, claimed_email, claimed_quantity "
            "FROM claim ORDER BY id"
        )
    ).fetchall()
    groups = {}
    for row in rows:
        key = (row.gift_id, row.claimed_email.strip().lower())
        group = groups.setdefault(key, {"id": row.id, "quantity": 0, "email": key[1]})
        group["quantity"] += row.claimed_quantity
        if group["id"] != row.id:
            connection.execute(
                sa.text("DELETE FROM claim WHERE id = :claim_id"),
                {"claim_id": row.id},
            )

    for (gift_id, _), group in groups.items():
        connection.execute(
            sa.text(
                "UPDATE claim SET claimed_email = :email, claimed_quantity = :quantity "
                "WHERE id = :claim_id"
            ),
            {"email": group["email"], "quantity": group["quantity"], "claim_id": group["id"]},
        )

    op.create_index(
        "uq_claim_gift_claimed_email",
        "claim",
        ["gift_id", "claimed_email"],
        unique=True,
    )


def downgrade():
    op.drop_index("uq_claim_gift_claimed_email", table_name="claim")
    with op.batch_alter_table("claim", schema=None) as batch_op:
        batch_op.drop_column("claimed_quantity")
