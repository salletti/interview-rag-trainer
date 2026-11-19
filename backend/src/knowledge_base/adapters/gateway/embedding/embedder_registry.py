"""Fournisseur d'embedders par profil (S9 ; port ``EmbedderProvider`` ;
ADR 0011 « la recherche charge le modèle de son profil »).

Une fabrique par profil configuré, appelée au PREMIER usage de ce profil,
puis la même instance : un processus ne construit que les modèles des index
qu'il sert, et les fabriques de production (``SentenceTransformersEmbedder.
lazy``) ne chargent les poids qu'au premier encodage. Pendant une migration,
l'API et le worker portent ainsi deux modèles (l'actif et celui de l'index
``BUILDING``) ; un modèle n'est jamais déchargé, un redémarrage du processus
libère celui de l'index retiré.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Self

from knowledge_base.adapters.gateway.embedding.sentence_transformers_embedder import (
    SentenceTransformersEmbedder,
)
from knowledge_base.domain.providers.embedder_provider import (
    TokenCountingEmbedder,
    UnknownEmbeddingProfileError,
)
from knowledge_base.domain.value_objects.embedding_profile import EmbeddingProfile

EmbedderFactory = Callable[[], TokenCountingEmbedder]


class EmbedderRegistry:
    def __init__(self, factories: Mapping[EmbeddingProfile, EmbedderFactory]) -> None:
        self._factories = dict(factories)
        self._built: dict[EmbeddingProfile, TokenCountingEmbedder] = {}
        # La recherche (boucle asyncio de l'API) et les threads d'encodage
        # peuvent demander le même profil ensemble : une seule construction.
        self._lock = threading.Lock()

    @classmethod
    def lazy_sentence_transformers(
        cls,
        profiles: Iterable[EmbeddingProfile],
        *,
        cache_dir: str | Path | None = None,
    ) -> Self:
        """Un ``SentenceTransformersEmbedder`` paresseux par profil : rien
        n'est chargé avant le premier encodage ou comptage de tokens."""

        def factory(profile: EmbeddingProfile) -> EmbedderFactory:
            return lambda: SentenceTransformersEmbedder.lazy(
                profile, cache_dir=cache_dir
            )

        return cls({profile: factory(profile) for profile in profiles})

    @property
    def profiles(self) -> frozenset[EmbeddingProfile]:
        return frozenset(self._factories)

    def for_profile(self, profile: EmbeddingProfile) -> TokenCountingEmbedder:
        with self._lock:
            built = self._built.get(profile)
            if built is not None:
                return built
            factory = self._factories.get(profile)
            if factory is None:
                raise UnknownEmbeddingProfileError(profile)
            built = factory()
            if built.profile != profile:
                # Ses vecteurs iraient dans la collection d'un autre profil.
                raise ValueError(
                    f"la fabrique du profil {profile.model}@{profile.revision[:8]} "
                    f"rend un modèle du profil {built.profile.model}@"
                    f"{built.profile.revision[:8]}"
                )
            self._built[profile] = built
            return built
