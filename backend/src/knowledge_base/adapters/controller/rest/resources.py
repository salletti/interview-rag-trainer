"""Ressources de l'API pour Knowledge Base, créées par ``runtime`` au
démarrage de l'application (``lifespan``).

Elles portent aussi le modèle d'embeddings de la recherche : **un** embedder
paresseux par jeu de ressources, que la route de recherche (``register``) et
la fabrique publique ``build_knowledge_search`` reçoivent toutes deux. Ainsi
un consommateur composé par ``runtime`` sur les mêmes ressources ne charge pas
le modèle (~1,1 Gio) une seconde fois. Il n'est pas un paramètre du
constructeur : ``runtime`` ne connaît pas ce type interne, et ses réglages
(cache) viennent de ``settings``, la seule source.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base.adapters.gateway.embedding.sentence_transformers_embedder import (
    SentenceTransformersEmbedder,
)
from knowledge_base.domain.value_objects.embedding_profile import MULTILINGUAL_E5_BASE
from knowledge_base.frameworks.configuration.settings import KnowledgeBaseSettings


@dataclass(frozen=True)
class KnowledgeBaseResources:
    session_factory: async_sessionmaker[AsyncSession]
    settings: KnowledgeBaseSettings
    # Recherche (S5) : client du processus, fermé par ``runtime``.
    qdrant_client: AsyncQdrantClient
    # Chargé à la première recherche (jamais à la construction : le démarrage
    # et ``/health/ready`` n'en dépendent pas), puis gardé.
    embedder: SentenceTransformersEmbedder = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        # ``frozen`` : seul moyen d'initialiser un champ dérivé des autres.
        object.__setattr__(
            self,
            "embedder",
            SentenceTransformersEmbedder.lazy(
                MULTILINGUAL_E5_BASE, cache_dir=self.settings.models_cache_dir
            ),
        )
