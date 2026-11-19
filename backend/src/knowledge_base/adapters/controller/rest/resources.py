"""Ressources de l'API pour Knowledge Base, créées par ``runtime`` au
démarrage de l'application (``lifespan``).

Elles portent aussi les modèles d'embeddings de la recherche : **un**
fournisseur par jeu de ressources (S9 : un modèle paresseux par profil du
catalogue des cibles, construit à la première recherche sur un index de ce
profil), que la route de recherche (``register``) et la fabrique publique
``build_knowledge_search`` reçoivent toutes deux. Ainsi un consommateur
composé par ``runtime`` sur les mêmes ressources ne charge pas un modèle
(~1,1 Gio pour multilingual-e5-base) une seconde fois. Il n'est pas un
paramètre du constructeur : ``runtime`` ne connaît pas ce type interne, et ses
réglages (cache) viennent de ``settings``, la seule source.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base.adapters.gateway.embedding.embedder_registry import (
    EmbedderRegistry,
)
from knowledge_base.frameworks.configuration.embedding_targets import (
    catalogued_profiles,
)
from knowledge_base.frameworks.configuration.settings import KnowledgeBaseSettings


@dataclass(frozen=True)
class KnowledgeBaseResources:
    session_factory: async_sessionmaker[AsyncSession]
    settings: KnowledgeBaseSettings
    # Recherche (S5) : client du processus, fermé par ``runtime``.
    qdrant_client: AsyncQdrantClient
    # Le modèle du profil de l'index actif, chargé à la première recherche
    # sur cet index (jamais à la construction : le démarrage et
    # ``/health/ready`` n'en dépendent pas), puis gardé.
    embedders: EmbedderRegistry = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # ``frozen`` : seul moyen d'initialiser un champ dérivé des autres.
        object.__setattr__(
            self,
            "embedders",
            EmbedderRegistry.lazy_sentence_transformers(
                catalogued_profiles(), cache_dir=self.settings.models_cache_dir
            ),
        )
