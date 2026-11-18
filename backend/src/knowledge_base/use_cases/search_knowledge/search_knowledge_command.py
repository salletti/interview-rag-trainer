from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from knowledge_base.domain.value_objects.search_filters import SearchFilters

# Bornes d'une requête (knowledge-base.md §14), partagées avec le schéma REST.
# Au-delà de 50 passages (jusqu'à ``max_passage_tokens`` chacun), le résultat
# n'est plus un contexte exploitable et coûte autant de lectures SQL.
MAX_SEARCH_LIMIT: Final = 50
# Une requête est une question, pas un document : 2 000 caractères, plus de
# vingt fois la plus longue requête du jeu de référence S5 (90), bornent la
# tokenisation faite à chaque requête (le modèle tronque à 512 tokens).
MAX_SEARCH_TEXT_LENGTH: Final = 2_000


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
        if len(self.text) > MAX_SEARCH_TEXT_LENGTH:
            raise ValueError(
                f"texte de recherche de {len(self.text)} caractères, au plus "
                f"{MAX_SEARCH_TEXT_LENGTH}"
            )
        if not 1 <= self.limit <= MAX_SEARCH_LIMIT:
            raise ValueError(
                f"limit doit être entre 1 et {MAX_SEARCH_LIMIT} : {self.limit}"
            )
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
