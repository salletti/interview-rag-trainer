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
    """L'index actif a été construit avec un profil dont le processus n'a pas
    le modèle : encoder la requête avec un autre modèle produirait des
    vecteurs incomparables (ADR 0011 §2), donc des résultats faux sans
    erreur."""

    def __init__(self, index_id: UUID, index_profile: EmbeddingProfile) -> None:
        super().__init__(
            f"index actif {index_id} construit avec le profil "
            f"{_describe(index_profile)}, aucun modèle de ce profil n'est "
            "configuré dans ce processus"
        )
        self.index_id = index_id
        self.index_profile = index_profile


def _describe(profile: EmbeddingProfile) -> str:
    return f"{profile.model}@{profile.revision[:8]} ({profile.dimension} dim.)"
