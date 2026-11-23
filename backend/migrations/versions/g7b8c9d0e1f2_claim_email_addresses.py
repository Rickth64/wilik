"""Replace claim names with email addresses

Revision ID: g7b8c9d0e1f2
Revises: c3d4e5f6a7b8
Create Date: 2026-11-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import re


revision = "g7b8c9d0e1f2"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    existing_claims = connection.execute(sa.text("SELECT id, claimed_by FROM claim")).fetchall()
    with op.batch_alter_table("claim", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("claimed_email", sa.String(length=255), nullable=False, server_default="")
        )
    with op.batch_alter_table("claim", schema=None) as batch_op:
        batch_op.alter_column("claimed_email", server_default=None)

    for claim_id, claimed_by in existing_claims:
        email = (claimed_by or "").strip().lower()
        if len(email) <= 255 and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            connection.execute(
                sa.text("UPDATE claim SET claimed_email = :email WHERE id = :claim_id"),
                {"email": email, "claim_id": claim_id},
            )
        else:
            connection.execute(
                sa.text("DELETE FROM claim WHERE id = :claim_id"),
                {"claim_id": claim_id},
            )

    with op.batch_alter_table("claim", schema=None) as batch_op:
        batch_op.drop_column("claimed_by")


def downgrade():
    with op.batch_alter_table("claim", schema=None) as batch_op:
        batch_op.drop_column("claimed_email")
        batch_op.add_column(sa.Column("claimed_by", sa.String(length=100), nullable=True))
