"""Refus de ``DropIndexCollection``."""

from __future__ import annotations

from typing import Final
from uuid import UUID

from knowledge_base.domain.entities.knowledge_corpus import IndexStatus
from knowledge_base.domain.value_objects.error_catalog import INDEX_STILL_LIVE


class IndexStillLiveError(Exception):
    """L'index est ``ACTIVE`` ou ``BUILDING`` : sa collection sert la
    recherche ou la migration, elle n'est jamais supprimée."""

    code: Final = INDEX_STILL_LIVE

    def __init__(self, index_id: UUID, status: IndexStatus) -> None:
        super().__init__(
            f"index {index_id} encore {status} : sa collection n'est pas supprimée"
        )
        self.index_id = index_id
        self.status = status
