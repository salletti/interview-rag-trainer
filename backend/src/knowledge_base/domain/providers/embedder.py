"""Port d'embeddings (ADR 0008 §4, ADR 0011 §1).

Les textes reçus sont bruts (``contextual_text`` d'un chunk, texte d'une
requête) : les préfixes de modèle (``passage: ``, ``query: ``) et la
normalisation du profil sont appliqués par l'adapter. Le calcul est long et
CPU : l'adapter le sort de la boucle asyncio.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from knowledge_base.domain.value_objects.embedding_profile import EmbeddingProfile


class Embedder(Protocol):
    @property
    def profile(self) -> EmbeddingProfile: ...

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        """Un vecteur par texte, dans l'ordre ; aucun texte, aucun appel."""
        ...

    async def embed_query(self, text: str) -> list[float]: ...
