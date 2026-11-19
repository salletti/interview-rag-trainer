"""knowledge_base document_versions source immutable (I1, revue S2 M-4)

Revision ID: 756db53032ab
Revises: 22df253e1e05
Create Date: 2026-11-18 14:00:00

I1 en base (docs/conception/bounded-contexts/knowledge-base.md §4.1) : un
trigger ``BEFORE UPDATE`` refuse toute modification de la source d'une
version (``number``, ``content_hash``, ``filename``, ``media_type``,
``size_bytes``, ``imported_at``) ; seuls l'état et la cause d'échec changent.

``CREATE TRIGGER`` prend un verrou SHARE ROW EXCLUSIVE sur
``document_versions`` le temps de l'instruction, sans parcours de la table :
les écritures concurrentes sur les versions attendent quelques millisecondes.
La fonction et le trigger ne sont pas dans les MetaData : ``alembic check``
ne compare ni l'un ni l'autre.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "756db53032ab"
down_revision: str | Sequence[str] | None = "22df253e1e05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTION = "knowledge_base.document_version_source_immutable"
TRIGGER = "trg_document_versions_source_immutable"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE FUNCTION {FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF (NEW.number, NEW.content_hash, NEW.filename, NEW.media_type,
                NEW.size_bytes, NEW.imported_at)
               IS DISTINCT FROM
               (OLD.number, OLD.content_hash, OLD.filename, OLD.media_type,
                OLD.size_bytes, OLD.imported_at)
            THEN
                RAISE EXCEPTION
                    'I1 : la source de la version % est immuable', OLD.id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER {TRIGGER} BEFORE UPDATE "
        "ON knowledge_base.document_versions FOR EACH ROW "
        f"EXECUTE FUNCTION {FUNCTION}()"
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER {TRIGGER} ON knowledge_base.document_versions")
    op.execute(f"DROP FUNCTION {FUNCTION}()")
