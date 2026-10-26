"""Хранение сообщения с карточкой таймера. Настройки семей не меняются."""
from alembic import op
import sqlalchemy as sa

revision = "a24c10260001"
down_revision = "1da3278e4719"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("card_message_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("events", "card_message_id")
