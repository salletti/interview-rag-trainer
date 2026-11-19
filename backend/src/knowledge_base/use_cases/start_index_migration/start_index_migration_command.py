from __future__ import annotations

from dataclasses import dataclass

from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy
from knowledge_base.domain.value_objects.embedding_profile import EmbeddingProfile


@dataclass(frozen=True)
class StartIndexMigration:
    """Profil et politique de l'index ``BUILDING`` à construire à côté de
    l'index actif (knowledge-base.md §13 ; ADR 0011 §4)."""

    profile: EmbeddingProfile
    chunking_policy: ChunkingPolicy
