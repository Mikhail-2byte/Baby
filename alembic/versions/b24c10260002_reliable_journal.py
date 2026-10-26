"""Журнал правок, защита повторной доставки, мягкие напоминания.

Настройки существующих семей не выключаются; простой профиль включается /quiet.
"""
from alembic import op
import sqlalchemy as sa

revision = "b24c10260002"
down_revision = "a24c10260001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("families", sa.Column("diapers_tracked", sa.Boolean(),
                                       server_default="1", nullable=False))
    op.add_column("families", sa.Column("feed_nudge_minutes", sa.Integer(),
                                       server_default="0", nullable=False))
    op.add_column("families", sa.Column("sleep_nudge_minutes", sa.Integer(),
                                       server_default="0", nullable=False))
    op.add_column("events", sa.Column("nudged_at", sa.DateTime(), nullable=True))
    op.create_table("processed_messages",
                    sa.Column("chat_id", sa.BigInteger(), primary_key=True),
                    sa.Column("message_id", sa.Integer(), primary_key=True),
                    sa.Column("created_at", sa.DateTime(), nullable=False,
                              server_default=sa.func.now()))
    op.create_table("record_revisions",
                    sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("child_id", sa.Integer(), sa.ForeignKey("children.id"), nullable=False),
                    sa.Column("actor_id", sa.Integer(), sa.ForeignKey("members.id")),
                    sa.Column("what", sa.String(1), nullable=False),
                    sa.Column("row_id", sa.Integer(), nullable=False),
                    sa.Column("before_json", sa.Text(), nullable=False),
                    sa.Column("after_json", sa.Text(), nullable=False),
                    sa.Column("created_at", sa.DateTime(), nullable=False,
                              server_default=sa.func.now()),
                    sa.Column("undone_at", sa.DateTime()),
                    sa.Column("undo_actor_id", sa.Integer(), sa.ForeignKey("members.id")))
    op.create_index("ix_record_revisions_child_id", "record_revisions", ["child_id"])
    op.create_index("ix_record_revisions_row_id", "record_revisions", ["row_id"])

    for table in ("events", "measurements"):
        with op.batch_alter_table(table, recreate="always",
                                  table_kwargs={"sqlite_autoincrement": True}):
            pass


def downgrade() -> None:
    op.drop_table("record_revisions")
    op.drop_table("processed_messages")
    op.drop_column("events", "nudged_at")
    for name in ("sleep_nudge_minutes", "feed_nudge_minutes", "diapers_tracked"):
        op.drop_column("families", name)
