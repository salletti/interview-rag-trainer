"""Port de la work queue durable vu par le domaine (S0 §4).

Seuls ``enqueue`` et ``cancel`` sont exposés : ils s'exécutent dans la
transaction du handler métier (même Unit of Work). Les autres transitions
(claim, heartbeat, finalisation, reprise) sont des opérations d'adapter.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol
from uuid import UUID

# Codes d'erreur stables écrits dans ``work_items.last_error_code`` (S0 §2).
# Les codes métier (``EMPTY_DOCUMENT``…) sont définis par les handlers.
TRANSIENT: Final = "TRANSIENT"
UNEXPECTED_ERROR: Final = "UNEXPECTED_ERROR"
UNKNOWN_KIND: Final = "UNKNOWN_KIND"
NOT_FINALIZED: Final = "NOT_FINALIZED"
TERMINAL_HOOK_FAILED: Final = "TERMINAL_HOOK_FAILED"
LEASE_EXPIRED: Final = "LEASE_EXPIRED"
LEASE_EXPIRED_MAX_ATTEMPTS: Final = "LEASE_EXPIRED_MAX_ATTEMPTS"

MAX_ERROR_MESSAGE_LENGTH: Final = 2000


@dataclass(frozen=True)
class FailureInfo:
    """Erreur d'une tentative : code stable et diagnostic sans secret.

    Le message est tronqué à la construction : la même borne s'applique à la
    colonne ``last_error_message`` et à ce que reçoit un hook d'échec métier.
    """

    code: str
    message: str = field(default="")

    def __post_init__(self) -> None:
        if len(self.message) > MAX_ERROR_MESSAGE_LENGTH:
            object.__setattr__(self, "message", self.message[:MAX_ERROR_MESSAGE_LENGTH])


class WorkQueue(Protocol):
    """Enregistre ou invalide un travail dans la transaction métier courante.
    N'ouvre ni ne valide jamais de transaction."""

    async def enqueue(
        self,
        kind: str,
        dedup_key: str,
        payload: Mapping[str, str] | None = None,
        correlation_id: UUID | None = None,
        *,
        max_attempts: int | None = None,
    ) -> UUID:
        """Identifiant de l'item ; celui de l'item existant si ``dedup_key``
        est déjà connue (y compris pour un item terminé)."""
        ...

    async def cancel(self, dedup_key: str) -> bool:
        """Vrai si un item ``PENDING`` ou ``RUNNING`` a été annulé."""
        ...
