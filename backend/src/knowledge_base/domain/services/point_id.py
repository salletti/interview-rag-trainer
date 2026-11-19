"""Identifiant déterministe d'un point Qdrant (knowledge-base.md §7.3 ; ADR 0010
§2) : le même chunk donne le même point dans un index, un autre dans un autre
index. Rejouer une indexation écrase donc ses points au lieu de les dupliquer.
"""

from __future__ import annotations

from uuid import UUID, uuid5


def point_id(index_id: UUID, chunk_id: UUID) -> UUID:
    return uuid5(index_id, str(chunk_id))
