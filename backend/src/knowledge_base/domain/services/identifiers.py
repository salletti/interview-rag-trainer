"""Identifiants déterministes des unités et chunks (knowledge-base.md §3.4).

UUIDv5 de (version, identité de la politique, rangs) : une ré-extraction
après panne produit les mêmes identifiants, donc les mêmes points Qdrant
(§7.3). ``policy_identity`` est ``ChunkingPolicy.identity`` (version et
tokenizer).
"""

from __future__ import annotations

from typing import Final
from uuid import UUID, uuid5

# Espace de noms propre à Knowledge Base ; ne jamais le changer : tous les
# identifiants déjà persistés en dépendent.
_NAMESPACE: Final = UUID("6f1f6a52-3c2e-5b8e-9d3a-7a4c1e0b2f90")


def unit_id(version_id: UUID, policy_identity: str, ordinal: int) -> UUID:
    return uuid5(_NAMESPACE, f"unit:{version_id}:{policy_identity}:{ordinal}")


def chunk_id(
    version_id: UUID, policy_identity: str, unit_ordinal: int, ordinal_in_unit: int
) -> UUID:
    return uuid5(
        _NAMESPACE,
        f"chunk:{version_id}:{policy_identity}:{unit_ordinal}:{ordinal_in_unit}",
    )
