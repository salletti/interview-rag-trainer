"""Prédicat SQL unique « version indexable » (S9 ; revue S9) : une version
``PUBLISHED`` ou ``EXTRACTED`` d'un document ``ACTIVE``, celle qu'un index
vivant doit couvrir (knowledge-base.md §13).

Partagé par la lecture de la migration (``SqlAlchemyIndexableVersionReader``)
et par la réconciliation (``SqlAlchemyReconciliationStore``) : le backfill,
la preuve de couverture et le rattrapage d'un index ``BUILDING`` portent sur
le même ensemble de versions.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import ColumnElement, and_

from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    VersionStatus,
)
from knowledge_base.frameworks.sqlalchemy.tables import document_versions, documents

INDEXABLE_VERSION_STATUSES: Final = (
    VersionStatus.PUBLISHED.value,
    VersionStatus.EXTRACTED.value,
)

# Jointure que le prédicat suppose : ``document_versions`` avec son document.
VERSION_DOCUMENT_JOIN: Final = documents.c.id == document_versions.c.document_id


def is_indexable_version() -> ColumnElement[bool]:
    """À combiner avec ``VERSION_DOCUMENT_JOIN``."""
    return and_(
        document_versions.c.status.in_(INDEXABLE_VERSION_STATUSES),
        documents.c.status == DocumentStatus.ACTIVE.value,
    )
