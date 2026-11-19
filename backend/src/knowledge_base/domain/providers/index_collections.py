"""Port de suppression de la collection d'un index retiré (S9 ;
knowledge-base.md §13 : « l'ancien index est retiré, puis sa collection
supprimée par un item de maintenance » ; ADR 0011 §4).

Port étroit, distinct de ``VectorIndex`` : seule la maintenance supprime une
collection entière ; l'adapter Qdrant implémente les deux.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class IndexCollections(Protocol):
    async def delete_collection(self, index_id: UUID) -> None:
        """Supprime la collection de l'index ; sans erreur si elle n'existe
        plus (rejeu)."""
        ...
