"""Port de la work queue durable vu par le domaine (S0 §4).

Seuls ``enqueue``, ``enqueue_again`` et ``cancel`` sont exposés : ils
s'exécutent dans la transaction du handler métier (même Unit of Work). Les
autres transitions (claim, heartbeat, finalisation, reprise) sont des
opérations d'adapter ; la réouverture d'un item ``FAILED`` (S8) passe par le
port ``FailedWorkRepository``.

Générations (S8) : ``dedup_key`` est unique pour toujours (S0 §2), un item
terminé ne se relance donc pas sous sa clé. Quand le même travail logique doit
être refait (points réécrits par une tentative zombie, manifest à revérifier),
``enqueue_again`` crée la génération suivante, ``<clé>:<n>`` ; la clé de base
et ses générations forment une **famille**, que ``cancel`` annule en entier.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final, Protocol
from uuid import UUID

# Codes d'erreur stables écrits dans ``work_items.last_error_code`` (S0 §2) :
# définis par le catalogue unique (S8), réexportés ici pour la work queue.
from knowledge_base.domain.value_objects.error_catalog import (
    LEASE_EXPIRED,
    LEASE_EXPIRED_MAX_ATTEMPTS,
    NOT_FINALIZED,
    TERMINAL_HOOK_FAILED,
    TRANSIENT,
    UNEXPECTED_ERROR,
    UNKNOWN_KIND,
)

__all__ = [
    "LEASE_EXPIRED",
    "LEASE_EXPIRED_MAX_ATTEMPTS",
    "MAX_ERROR_MESSAGE_LENGTH",
    "NOT_FINALIZED",
    "TERMINAL_HOOK_FAILED",
    "TRANSIENT",
    "UNEXPECTED_ERROR",
    "UNKNOWN_KIND",
    "FailureInfo",
    "WorkQueue",
]

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
        delay: timedelta | None = None,
    ) -> UUID:
        """Identifiant de l'item ; celui de l'item existant si ``dedup_key``
        est déjà connue (y compris pour un item terminé). ``delay`` : l'item
        n'est disponible qu'après ce délai (S9 : suppression différée d'une
        collection retirée) ; immédiatement sinon."""
        ...

    async def enqueue_again(
        self,
        kind: str,
        dedup_key: str,
        payload: Mapping[str, str] | None = None,
        correlation_id: UUID | None = None,
    ) -> UUID:
        """Travail à (re)faire sous la famille de ``dedup_key`` : l'item de la
        famille encore ``PENDING`` ou ``RUNNING`` s'il y en a un (aucun
        doublon) ; sinon un nouvel item, sous ``dedup_key`` si la famille est
        vide, sous ``<dedup_key>:<n>`` (génération suivante) sinon."""
        ...

    async def cancel(self, dedup_key: str) -> bool:
        """Annule les items ``PENDING`` ou ``RUNNING`` de la famille de
        ``dedup_key`` (la clé et ses générations ``<clé>:<n>``). Vrai si au
        moins un item a été annulé."""
        ...
