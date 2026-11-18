"""Échecs de la vérification de l'index (knowledge-base.md §9 ; ADR 0010 §3).

- ``IndexManifestMismatchError`` : un point manquant ou un ``chunk_hash``
  différent à la relecture. **Transitoire** : la tentative suivante réécrit
  tous les points (upserts idempotents) puis revérifie.
- ``CollectionMismatchError`` : collection absente, ou de dimension ou de
  distance différentes du profil, ou vecteurs produits d'une autre dimension.
  **Terminale** : réécrire ne corrige rien, la collection ou le modèle sont à
  reprendre.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

COLLECTION_MISMATCH: Final = "COLLECTION_MISMATCH"


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
