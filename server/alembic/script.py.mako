"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Every Crashlens revision MUST implement both upgrade() and downgrade() with
explicit, reversible operations. Do not leave downgrade() as a no-op.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "raise NotImplementedError(\"write the explicit upgrade\")"}


def downgrade() -> None:
    ${downgrades if downgrades else "raise NotImplementedError(\"write the explicit reversible downgrade\")"}
