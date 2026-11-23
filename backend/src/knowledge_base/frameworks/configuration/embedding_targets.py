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
``DEFAULT_CHUNKING_POLICY``, ADR 0009 §3). Les profils (Value Objects, avec
leurs révisions) sont définis dans le domaine
(``domain/value_objects/embedding_profile``) ; ce module ne porte que le
catalogue des cibles, choisi d'après le benchmark de S9
(knowledge-base-embedding-benchmark.md).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from knowledge_base.domain.exceptions.corpus_errors import IndexOperationRefusedError
from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy
from knowledge_base.domain.value_objects.embedding_profile import (
    MULTILINGUAL_E5_BASE,
    MULTILINGUAL_E5_SMALL,
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


class UnknownMigrationTargetError(IndexOperationRefusedError, LookupError):
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
