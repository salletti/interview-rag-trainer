from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from knowledge_base.domain.value_objects.search_filters import SearchFilters


@dataclass(frozen=True)
class SearchKnowledge:
    """Query de recherche sémantique (knowledge-base.md §12, §14). Les filtres
    vides n'excluent rien ; leur sémantique est celle de ``SearchFilters``."""

    text: str
    limit: int = 5
    language: str | None = None
    topics: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    document_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("texte de recherche vide")
        if self.limit < 1:
            raise ValueError(f"limit doit être au moins 1 : {self.limit}")
        # Construire les filtres les valide (types de document connus) : une
        # query invalide est refusée avant tout accès à l'index.
        _ = self.filters

    @property
    def filters(self) -> SearchFilters:
        return SearchFilters(
            language=self.language,
            topics=self.topics,
            kinds=self.kinds,
            document_ids=self.document_ids,
        )
