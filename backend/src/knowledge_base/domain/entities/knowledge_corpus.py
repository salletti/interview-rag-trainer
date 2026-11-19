"""Agrégat ``KnowledgeCorpus`` et entité interne ``SearchIndex``
(knowledge-base.md §3.2, §5.4, §13 ; ADR 0011 ; I10, I11).

Singleton en V1 (``CORPUS_ID``). Les invariants « au plus un index
``ACTIVE`` » (I10) et « au plus un index ``BUILDING`` » (une seule migration
en cours, revue S9) sont portés ici, doublés chacun d'un index unique partiel
en base ; le contrôle de couverture d'I11 est calculé par le use
case d'activation et transmis (``coverage_complete``). La ligne du corpus sert
de barrière blue/green : ``FOR SHARE`` à l'extraction et à la publication,
``FOR UPDATE`` à l'activation (le verrou est l'affaire du dépôt).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Final, Self
from uuid import UUID

from knowledge_base.domain.exceptions.corpus_errors import (
    DuplicateIndexError,
    IncompleteCoverageError,
    IndexAlreadyBuildingError,
    InvalidIndexTransitionError,
    UnknownIndexError,
)
from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy
from knowledge_base.domain.value_objects.embedding_profile import EmbeddingProfile

# Identité du corpus unique de V1 (O3) ; ne jamais la changer : elle est
# persistée.
CORPUS_ID: Final = UUID("2f7c4d1e-8b3a-5c6d-9e0f-1a2b3c4d5e6f")


class IndexStatus(StrEnum):
    BUILDING = "BUILDING"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


@dataclass(frozen=True)
class SearchIndex:
    """Génération d'index : un profil, une politique, une collection (dont le
    nom est calculé par l'adapter)."""

    index_id: UUID
    profile: EmbeddingProfile
    chunking_policy: ChunkingPolicy
    status: IndexStatus
    created_at: datetime
    activated_at: datetime | None
    retired_at: datetime | None


class KnowledgeCorpus:
    """Aggregate Root. Toute opération refusée lève une
    ``KnowledgeCorpusError`` sans aucun effet."""

    def __init__(
        self,
        *,
        corpus_id: UUID,
        active_index_id: UUID | None,
        version: int,
        indexes: Iterable[SearchIndex],
    ) -> None:
        self._corpus_id = corpus_id
        self._active_index_id = active_index_id
        self._version = version
        self._indexes = list(indexes)

    @classmethod
    def empty(cls) -> Self:
        return cls(corpus_id=CORPUS_ID, active_index_id=None, version=0, indexes=())

    @classmethod
    def restore(
        cls,
        *,
        corpus_id: UUID,
        active_index_id: UUID | None,
        version: int,
        indexes: Iterable[SearchIndex],
    ) -> Self:
        """Réhydratation par la persistance, sans contrôle ; ``indexes`` dans
        l'ordre de création."""
        return cls(
            corpus_id=corpus_id,
            active_index_id=active_index_id,
            version=version,
            indexes=indexes,
        )

    # --- lecture ----------------------------------------------------------------

    @property
    def corpus_id(self) -> UUID:
        return self._corpus_id

    @property
    def active_index_id(self) -> UUID | None:
        return self._active_index_id

    @property
    def version(self) -> int:
        """Verrou optimiste : incrémenté par la persistance, jamais ici."""
        return self._version

    @property
    def indexes(self) -> tuple[SearchIndex, ...]:
        return tuple(self._indexes)

    def get_index(self, index_id: UUID) -> SearchIndex:
        return self._indexes[self._position(index_id)]

    def active_index(self) -> SearchIndex | None:
        if self._active_index_id is None:
            return None
        return self.get_index(self._active_index_id)

    def building_index(self) -> SearchIndex | None:
        """L'index en construction, s'il y en a un (au plus un)."""
        return next(
            (i for i in self._indexes if i.status is IndexStatus.BUILDING), None
        )

    def live_indexes(self) -> tuple[SearchIndex, ...]:
        """Index ``ACTIVE`` et ``BUILDING`` : ceux qu'une extraction alimente
        (§13)."""
        return tuple(i for i in self._indexes if i.status is not IndexStatus.RETIRED)

    # --- opérations ---------------------------------------------------------------

    def provision_index(
        self,
        *,
        index_id: UUID,
        profile: EmbeddingProfile,
        chunking_policy: ChunkingPolicy,
        now: datetime,
    ) -> SearchIndex:
        """Nouvel index ``BUILDING`` ; refusé si l'identifiant existe ou si
        un autre index est déjà en construction."""
        if any(i.index_id == index_id for i in self._indexes):
            raise DuplicateIndexError(index_id)
        building = self.building_index()
        if building is not None:
            raise IndexAlreadyBuildingError(building.index_id)
        index = SearchIndex(
            index_id=index_id,
            profile=profile,
            chunking_policy=chunking_policy,
            status=IndexStatus.BUILDING,
            created_at=now,
            activated_at=None,
            retired_at=None,
        )
        self._indexes.append(index)
        return index

    def activate_index(
        self, index_id: UUID, *, now: datetime, coverage_complete: bool
    ) -> None:
        """``BUILDING`` -> ``ACTIVE`` ; l'ancien actif devient ``RETIRED``
        (I10). Les contrôles précèdent toute modification."""
        position = self._position(index_id)
        self._ensure_building(self._indexes[position], "activation")
        if not coverage_complete:
            raise IncompleteCoverageError(index_id)
        previous = self._active_index_id
        if previous is not None:
            old = self._position(previous)
            self._indexes[old] = replace(
                self._indexes[old], status=IndexStatus.RETIRED, retired_at=now
            )
        self._indexes[position] = replace(
            self._indexes[position], status=IndexStatus.ACTIVE, activated_at=now
        )
        self._active_index_id = index_id

    def retire_index(self, index_id: UUID, *, now: datetime) -> None:
        """Abandon d'un index ``BUILDING`` ; l'index actif ne se retire que
        par l'activation d'un autre."""
        position = self._position(index_id)
        self._ensure_building(self._indexes[position], "retrait")
        self._indexes[position] = replace(
            self._indexes[position], status=IndexStatus.RETIRED, retired_at=now
        )

    # --- interne ------------------------------------------------------------------

    def _position(self, index_id: UUID) -> int:
        for position, index in enumerate(self._indexes):
            if index.index_id == index_id:
                return position
        raise UnknownIndexError(index_id)

    @staticmethod
    def _ensure_building(index: SearchIndex, operation: str) -> None:
        if index.status is not IndexStatus.BUILDING:
            raise InvalidIndexTransitionError(index.index_id, index.status, operation)
