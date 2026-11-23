"""Agrégat ``Indexation`` : fait de couverture « cette version est (ou doit
être) présente dans cet index » (knowledge-base.md §3.3, §5.3 ; I8).

Sans lease ni tentative : ce sont ceux du work item ``index_version`` qui la
traite. ``COMPLETED`` n'est atteint qu'avec l'empreinte d'un manifest vérifié
dans l'index (I8) ; la vérification elle-même est l'affaire du use case.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from knowledge_base.domain.exceptions.indexation_errors import (
    InvalidIndexationTransitionError,
)
from knowledge_base.domain.value_objects.failure_reason import FailureReason


class IndexationStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Indexation:
    """Toute transition refusée lève ``InvalidIndexationTransitionError`` sans
    aucun effet."""

    def __init__(
        self,
        *,
        version_id: UUID,
        index_id: UUID,
        status: IndexationStatus,
        chunking_policy_version: str,
        manifest_digest: str | None,
        point_count: int | None,
        completed_at: datetime | None,
        failure: FailureReason | None,
    ) -> None:
        self._version_id = version_id
        self._index_id = index_id
        self._status = status
        self._chunking_policy_version = chunking_policy_version
        self._manifest_digest = manifest_digest
        self._point_count = point_count
        self._completed_at = completed_at
        self._failure = failure

    @classmethod
    def pending(
        cls, *, version_id: UUID, index_id: UUID, chunking_policy_version: str
    ) -> Self:
        return cls(
            version_id=version_id,
            index_id=index_id,
            status=IndexationStatus.PENDING,
            chunking_policy_version=chunking_policy_version,
            manifest_digest=None,
            point_count=None,
            completed_at=None,
            failure=None,
        )

    @classmethod
    def restore(
        cls,
        *,
        version_id: UUID,
        index_id: UUID,
        status: IndexationStatus,
        chunking_policy_version: str,
        manifest_digest: str | None,
        point_count: int | None,
        completed_at: datetime | None,
        failure: FailureReason | None,
    ) -> Self:
        """Réhydratation par la persistance, sans contrôle."""
        return cls(
            version_id=version_id,
            index_id=index_id,
            status=status,
            chunking_policy_version=chunking_policy_version,
            manifest_digest=manifest_digest,
            point_count=point_count,
            completed_at=completed_at,
            failure=failure,
        )

    # --- lecture ----------------------------------------------------------------

    @property
    def version_id(self) -> UUID:
        return self._version_id

    @property
    def index_id(self) -> UUID:
        return self._index_id

    @property
    def status(self) -> IndexationStatus:
        return self._status

    @property
    def chunking_policy_version(self) -> str:
        return self._chunking_policy_version

    @property
    def manifest_digest(self) -> str | None:
        return self._manifest_digest

    @property
    def point_count(self) -> int | None:
        return self._point_count

    @property
    def completed_at(self) -> datetime | None:
        return self._completed_at

    @property
    def failure(self) -> FailureReason | None:
        return self._failure

    # --- transitions (§5.3) -------------------------------------------------------

    def complete(
        self, *, manifest_digest: str, point_count: int, completed_at: datetime
    ) -> None:
        self._ensure_status("complete", IndexationStatus.PENDING)
        if not manifest_digest.strip():
            raise ValueError("empreinte de manifest vide")
        if point_count < 0:
            raise ValueError(f"point_count négatif : {point_count}")
        self._status = IndexationStatus.COMPLETED
        self._manifest_digest = manifest_digest
        self._point_count = point_count
        self._completed_at = completed_at

    def fail(self, reason: FailureReason) -> None:
        self._ensure_status("fail", IndexationStatus.PENDING)
        self._status = IndexationStatus.FAILED
        self._failure = reason

    def retry(self) -> None:
        self._ensure_status("retry", IndexationStatus.FAILED)
        self._status = IndexationStatus.PENDING
        self._failure = None

    def reopen(self) -> None:
        """``COMPLETED`` -> ``PENDING`` : le manifest vérifié ne correspond plus
        à l'index (réconciliation, §9). Arête réservée à la réconciliation,
        distincte de ``retry`` (``FAILED`` -> ``PENDING``) : le manifest, le
        nombre de points et la date sont effacés (I8)."""
        self._ensure_status("reopen", IndexationStatus.COMPLETED)
        self._status = IndexationStatus.PENDING
        self._manifest_digest = None
        self._point_count = None
        self._completed_at = None

    def cancel(self) -> None:
        self._ensure_status("cancel", IndexationStatus.PENDING, IndexationStatus.FAILED)
        self._status = IndexationStatus.CANCELLED
        self._failure = None

    def _ensure_status(self, operation: str, *allowed: IndexationStatus) -> None:
        if self._status not in allowed:
            raise InvalidIndexationTransitionError(
                self._version_id, self._index_id, self._status, operation
            )
