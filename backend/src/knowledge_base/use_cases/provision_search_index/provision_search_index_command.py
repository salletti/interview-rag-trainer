from __future__ import annotations

from dataclasses import dataclass

from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy
from knowledge_base.domain.value_objects.embedding_profile import EmbeddingProfile


@dataclass(frozen=True)
class ProvisionSearchIndex:
    """Profil et politique du premier index du corpus (knowledge-base.md §13)."""

    profile: EmbeddingProfile
    chunking_policy: ChunkingPolicy
