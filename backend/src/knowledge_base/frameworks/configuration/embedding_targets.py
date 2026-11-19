"""Cibles de migration d'index : un nom d'exploitation -> un profil
d'embeddings et la politique de découpage construite pour lui (S9 ;
knowledge-base.md §13 ; ADR 0011 §1, §6, §7).

``python -m runtime.index_migration start <nom>`` démarre une migration vers
une cible du catalogue ; l'API et les workers savent charger le modèle de
chaque cible (``EmbedderRegistry``), à son premier usage seulement.

Chaque politique est comptée avec le tokenizer de son profil, à la révision
épinglée (identité ``<famille>-<révision courte>`` : un même nom de modèle à
une autre révision est un autre tokenizer). Son budget est la longueur
maximale du profil moins 2 tokens spéciaux et les tokens du préfixe de
passage, mesurés sur le tokenizer réel (méthode de
``DEFAULT_CHUNKING_POLICY``, ADR 0009 §3). Les profils et révisions viennent
du benchmark de S9 (knowledge-base-embedding-benchmark.md).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy
from knowledge_base.domain.value_objects.embedding_profile import (
    MULTILINGUAL_E5_BASE,
    Distance,
    EmbeddingProfile,
)
from knowledge_base.frameworks.configuration.chunking import DEFAULT_CHUNKING_POLICY


@dataclass(frozen=True)
class MigrationTarget:
    profile: EmbeddingProfile
    chunking_policy: ChunkingPolicy

    def __post_init__(self) -> None:
        if self.chunking_policy.tokenizer != self.profile.tokenizer:
            raise ValueError(
                f"politique {self.chunking_policy.identity} comptée avec un autre "
                f"tokenizer que celui du profil ({self.profile.tokenizer})"
            )
        if self.chunking_policy.max_tokens >= self.profile.max_tokens:
            raise ValueError(
                f"budget {self.chunking_policy.max_tokens} >= longueur maximale "
                f"du profil {self.profile.max_tokens}"
            )


MULTILINGUAL_E5_SMALL_REVISION: Final = "614241f622f53c4eeff9890bdc4f31cfecc418b3"

# Même famille que l'index initial (XLM-R, préfixes « query: »/« passage: »,
# 512 tokens), 384 dimensions. Son tokenizer est compté sous sa propre
# identité : ``tokenizer.json`` diffère de celui de multilingual-e5-base à sa
# révision (empreintes relevées le 2026-11-18), même si le vocabulaire
# SentencePiece est identique ; le même budget peut donc découper autrement.
MULTILINGUAL_E5_SMALL: Final = EmbeddingProfile(
    model="intfloat/multilingual-e5-small",
    revision=MULTILINGUAL_E5_SMALL_REVISION,
    dimension=384,
    distance=Distance.COSINE,
    normalize=True,
    query_prefix="query: ",
    passage_prefix="passage: ",
    max_tokens=512,
    tokenizer=f"xlmr-{MULTILINGUAL_E5_SMALL_REVISION[:8]}",
)

CATALOGUE: Final[Mapping[str, MigrationTarget]] = MappingProxyType(
    {
        "multilingual-e5-base": MigrationTarget(
            MULTILINGUAL_E5_BASE, DEFAULT_CHUNKING_POLICY
        ),
        "multilingual-e5-small": MigrationTarget(
            MULTILINGUAL_E5_SMALL,
            ChunkingPolicy(
                version=DEFAULT_CHUNKING_POLICY.version,
                tokenizer=MULTILINGUAL_E5_SMALL.tokenizer,
                max_tokens=DEFAULT_CHUNKING_POLICY.max_tokens,
            ),
        ),
    }
)


class UnknownMigrationTargetError(LookupError):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"cible de migration inconnue : {name!r} ; cibles du catalogue : "
            f"{', '.join(sorted(CATALOGUE))}"
        )
        self.name = name


def migration_target(name: str) -> MigrationTarget:
    try:
        return CATALOGUE[name]
    except KeyError:
        raise UnknownMigrationTargetError(name) from None


def catalogued_profiles() -> tuple[EmbeddingProfile, ...]:
    """Les profils que l'API et les workers savent charger."""
    return tuple(target.profile for target in CATALOGUE.values())
