"""knowledge_base corpus active index foreign key (S4, revue)

Revision ID: c43c7e850745
Revises: 448949d0e134
Create Date: 2026-11-18 18:00:00

Correction de la revue S4 : ``knowledge_corpus.active_index_id`` désigne un
index **de ce corpus** (knowledge-base.md §3.2 ; I10). Clé étrangère composite
``(corpus_id, active_index_id) → search_indexes(corpus_id, index_id)``, avec
la contrainte UNIQUE ``(corpus_id, index_id)`` qu'elle exige (``index_id`` est
déjà la clé primaire : la contrainte n'ajoute aucune restriction, seulement la
cible de la clé étrangère).

La clé étrangère est ``DEFERRABLE INITIALLY DEFERRED`` : la référence est
circulaire (``search_indexes.corpus_id → knowledge_corpus``), et le
provisionnement insère la ligne du corpus, déjà pourvue de son index actif,
avant la ligne de l'index ; le contrôle a lieu au commit. Un
``active_index_id`` NULL (corpus sans index actif) n'est pas contrôlé
(``MATCH SIMPLE``).

Verrous :

- ``search_indexes`` : ``ADD CONSTRAINT … UNIQUE`` construit un index sous
  verrou ACCESS EXCLUSIVE (lectures et écritures bloquées pendant la
  construction, proportionnelle au nombre d'index : une poignée de lignes) ;
- ``knowledge_corpus`` et ``search_indexes`` : la clé étrangère prend SHARE
  ROW EXCLUSIVE sur les deux tables et valide la ligne unique du corpus.

Durée : quelques millisecondes (tables de quelques lignes par construction).
Les lignes existantes provisionnées par S4 satisfont la contrainte : l'index
actif appartient toujours au corpus singleton.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c43c7e850745"
down_revision: str | Sequence[str] | None = "448949d0e134"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "knowledge_base"


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_search_indexes_corpus_id_index_id",
        "search_indexes",
        ["corpus_id", "index_id"],
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_knowledge_corpus_active_index",
        "knowledge_corpus",
        "search_indexes",
        ["corpus_id", "active_index_id"],
        ["corpus_id", "index_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_knowledge_corpus_active_index",
        "knowledge_corpus",
        type_="foreignkey",
        schema=SCHEMA,
    )
    op.drop_constraint(
        "uq_search_indexes_corpus_id_index_id",
        "search_indexes",
        type_="unique",
        schema=SCHEMA,
    )
