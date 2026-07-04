"""issue_comments.author nullable so ON DELETE SET NULL is coherent

Revision ID: 0004_comment_author_nullable
Revises: 0003_partition_fn_secdef
Create Date: 2026-07-04

Every Crashlens revision MUST implement both upgrade() and downgrade() with
explicit, reversible operations. Do not leave downgrade() as a no-op.

WHAT THIS REVISION DOES
-----------------------
Fixes a schema contradiction introduced by revision 0001 and confirmed during
the W3-03 governor review: ``issue_comments.author`` was declared NOT NULL
while its foreign key ``fk_comments_author`` is ``ON DELETE SET NULL``. If a
user who authored a comment were ever deleted, PostgreSQL would try to SET the
column to NULL and immediately violate the NOT NULL constraint, so the user
DELETE itself would fail. The two declarations cannot both stand; the SET NULL
intent (a deleted user leaves their comments behind, attributed to no one) is
the designed behavior, so this revision drops the NOT NULL:

    ALTER TABLE issue_comments ALTER COLUMN author DROP NOT NULL;

The application read path treats a NULL author as "former teammate": the
comments API surfaces ``author_id``/``author_email`` as null and the dashboard
renders a plain-language placeholder. Nothing ever INSERTS a NULL author (the
route always stamps the verified session user); NULL arises only via the FK
action on user deletion.

DOWNGRADE IS LOSSY FOR ORPHANED COMMENTS - DELIBERATELY
-------------------------------------------------------
The downgrade must restore NOT NULL, but any comment whose author was deleted
in the meantime now holds author NULL and would make ``SET NOT NULL`` fail.
There is no honest value to backfill: the authoring user row is gone, and
inventing a placeholder user (or reassigning to an admin) would fabricate
attribution. Deleting the orphaned comments first is therefore the ONLY
coherent reversal:

    DELETE FROM issue_comments WHERE author IS NULL;
    ALTER TABLE issue_comments ALTER COLUMN author SET NOT NULL;

Anyone running this downgrade should know those rows are unrecoverable after
it: the pre-0004 schema simply cannot represent a comment without an author.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_comment_author_nullable"
down_revision: str | None = "0003_partition_fn_secdef"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Make the column match its own FK action (ON DELETE SET NULL): a deleted
    # user leaves the comment behind with author NULL instead of blowing up
    # the user DELETE on the NOT NULL constraint.
    op.execute("ALTER TABLE issue_comments ALTER COLUMN author DROP NOT NULL")


def downgrade() -> None:
    # LOSSY: comments orphaned by a user deletion (author NULL) cannot exist
    # under the pre-0004 NOT NULL schema and there is no honest author value
    # to backfill, so they are deleted before the constraint is restored. See
    # the module docstring for why this is the only coherent reversal.
    op.execute("DELETE FROM issue_comments WHERE author IS NULL")
    op.execute("ALTER TABLE issue_comments ALTER COLUMN author SET NOT NULL")
