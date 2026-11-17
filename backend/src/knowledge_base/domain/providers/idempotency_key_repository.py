"""Port des clés d'idempotence HTTP (002, « Erreurs et idempotence »)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class IdempotencyRecord:
    """Résultat d'un import, rejoué tel quel pour la même clé et la même
    empreinte de requête."""

    key: str
    fingerprint: str
    document_id: UUID
    version_id: UUID
    number: int
    created: bool


class IdempotencyKeyTakenError(Exception):
    """La clé a été enregistrée par une transaction concurrente, commitée
    entre la lecture et l'écriture."""

    def __init__(self, key: str) -> None:
        super().__init__(
            f"clé d'idempotence {key!r} enregistrée par une requête concurrente"
        )
        self.key = key


class IdempotencyKeyRepository(Protocol):
    async def get(self, key: str) -> IdempotencyRecord | None: ...

    async def add(self, record: IdempotencyRecord) -> None:
        """Lève ``IdempotencyKeyTakenError`` si la clé existe déjà."""
        ...
