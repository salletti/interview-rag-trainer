"""Filtres d'une recherche (knowledge-base.md §12 étape 3, §14).

Ils portent sur les métadonnées du document de chaque point de l'index. Vide
signifie « sans restriction ». Sémantique :

- ``language`` : langue du document ;
- ``topics`` : le document porte **tous** ces sujets ;
- ``kinds`` : le type du document est **l'un** de ceux-ci (valeurs de
  ``DocumentKind``) ;
- ``document_ids`` : le document est **l'un** de ceux-ci.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from knowledge_base.domain.value_objects.document_kind import DocumentKind


@dataclass(frozen=True)
class SearchFilters:
    language: str | None = None
    topics: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    document_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        known = {kind.value for kind in DocumentKind}
        unknown = [kind for kind in self.kinds if kind not in known]
        if unknown:
            raise ValueError(
                f"type de document inconnu dans le filtre : {unknown} "
                f"(attendus : {sorted(known)})"
            )
