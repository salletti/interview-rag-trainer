"""Échecs de la vérification de l'index (knowledge-base.md §9 ; ADR 0010 §3).

- ``IndexManifestMismatchError`` : un point manquant ou un ``chunk_hash``
  différent à la relecture. **Transitoire** : la tentative suivante réécrit
  tous les points (upserts idempotents) puis revérifie.
- ``CollectionMismatchError`` : collection absente, ou de dimension ou de
  distance différentes du profil, ou vecteurs produits d'une autre dimension.
  **Terminale** : réécrire ne corrige rien, la collection ou le modèle sont à
  reprendre. Depuis S9, aussi un profil d'index que le worker ne sait pas
  charger.
- ``ChunkBatchMissingError`` (S9) : l'indexation vise une politique dont la
  version n'a pas de lot (rechunking non fait). **Terminale** : réindexer ne
  produit pas le lot ; rejouable une fois le rechunking fait.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from knowledge_base.domain.value_objects.error_catalog import (
    CHUNK_BATCH_MISSING,
    COLLECTION_MISMATCH,
)

__all__ = [
    "CHUNK_BATCH_MISSING",
    "COLLECTION_MISMATCH",
    "ChunkBatchMissingError",
    "CollectionMismatchError",
    "IndexManifestMismatchError",
]


class IndexManifestMismatchError(Exception):
    def __init__(
        self, version_id: UUID, index_id: UUID, *, missing: int, different: int
    ) -> None:
        super().__init__(
            f"manifest de la version {version_id} dans l'index {index_id} non "
            f"vérifié : {missing} point(s) manquant(s), {different} empreinte(s) "
            "différente(s)"
        )
        self.version_id = version_id
        self.index_id = index_id
        self.missing = missing
        self.different = different


class CollectionMismatchError(Exception):
    code: Final = COLLECTION_MISMATCH

    def __init__(self, index_id: UUID, reason: str) -> None:
        super().__init__(f"index {index_id} non conforme à son profil : {reason}")
        self.index_id = index_id
        self.reason = reason


class ChunkBatchMissingError(Exception):
    code: Final = CHUNK_BATCH_MISSING

    def __init__(self, version_id: UUID, index_id: UUID, policy_version: str) -> None:
        super().__init__(
            f"version {version_id} sans lot de chunks de la politique "
            f"{policy_version} de l'index {index_id} : rechunking non fait"
        )
        self.version_id = version_id
        self.index_id = index_id
        self.policy_version = policy_version
