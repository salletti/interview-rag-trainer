"""Port du choix de l'``Embedder`` d'après le profil d'un index (S9 ; ADR 0011
« la recherche lit l'index ACTIVE et charge le modèle de son profil »,
« pendant une migration, deux modèles peuvent être chargés »).

Jusqu'en S8, un processus n'avait qu'un ``Embedder`` : un index d'un autre
profil échouait en ``COLLECTION_MISMATCH`` (indexation) ou
``SEARCH_PROFILE_MISMATCH`` (recherche). Le fournisseur rend le modèle du
profil demandé ; il ne sait charger que les profils configurés dans le
processus, et le dit (``UnknownEmbeddingProfileError``) plutôt que de rendre
un autre modèle, dont les vecteurs seraient incomparables (ADR 0011 §2).
"""

from __future__ import annotations

from typing import Protocol

from knowledge_base.domain.providers.embedder import Embedder
from knowledge_base.domain.providers.token_counter import TokenCounter
from knowledge_base.domain.value_objects.embedding_profile import EmbeddingProfile


class TokenCountingEmbedder(Embedder, Protocol):
    """Un ``Embedder`` et le tokenizer de son profil, qui mesure le budget des
    chunks (ADR 0009 §3) et la question d'un passage (§12 étape 6)."""

    @property
    def token_counter(self) -> TokenCounter: ...


class UnknownEmbeddingProfileError(LookupError):
    """Le processus ne sait pas charger ce profil (absent de sa
    configuration)."""

    def __init__(self, profile: EmbeddingProfile) -> None:
        super().__init__(
            f"aucun modèle configuré dans ce processus pour le profil "
            f"{profile.model}@{profile.revision[:8]} ({profile.dimension} dim.)"
        )
        self.profile = profile


class EmbedderProvider(Protocol):
    def for_profile(self, profile: EmbeddingProfile) -> TokenCountingEmbedder:
        """Le modèle de ``profile``, toujours le même pour un même profil.
        Synchrone et léger : le modèle rendu se charge à son premier usage.
        Lève ``UnknownEmbeddingProfileError`` pour un profil non configuré."""
        ...
