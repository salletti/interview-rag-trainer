"""Refus de la recherche : l'index actif ne permet pas de répondre."""

from __future__ import annotations

from uuid import UUID

from knowledge_base.domain.value_objects.embedding_profile import EmbeddingProfile


class NoActiveSearchIndexError(Exception):
    """Le corpus n'a pas d'index actif (provisionnement non fait)."""

    def __init__(self) -> None:
        super().__init__(
            "aucun index de recherche actif : le provisionnement "
            "(python -m runtime.provision) n'a pas été exécuté"
        )


class SearchProfileMismatchError(Exception):
    """L'index actif a été construit avec un autre profil que celui de
    l'embedder de la requête : ses vecteurs ne sont pas comparables (ADR 0011
    §2). Chercher quand même rendrait des résultats faux sans erreur."""

    def __init__(
        self,
        index_id: UUID,
        index_profile: EmbeddingProfile,
        query_profile: EmbeddingProfile,
    ) -> None:
        super().__init__(
            f"index actif {index_id} construit avec le profil "
            f"{_describe(index_profile)}, requête encodée avec "
            f"{_describe(query_profile)}"
        )
        self.index_id = index_id
        self.index_profile = index_profile
        self.query_profile = query_profile


def _describe(profile: EmbeddingProfile) -> str:
    return f"{profile.model}@{profile.revision[:8]} ({profile.dimension} dim.)"
