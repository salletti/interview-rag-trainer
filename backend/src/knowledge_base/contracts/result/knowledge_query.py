"""Requête du contrat ``KnowledgeSearch`` (knowledge-base.md §14).

Filtres vides : aucune restriction. ``topics`` : le document porte tous ces
sujets ; ``kinds`` (``COURSE``, ``QA_SHEET``, ``REVISION_SHEET``,
``TECHNICAL``) et ``document_ids`` : l'un d'eux ; ``language`` : langue du
document.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class KnowledgeQuery:
    text: str
    limit: int = 5
    language: str | None = None
    topics: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    document_ids: tuple[UUID, ...] = ()
