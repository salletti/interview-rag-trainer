"""Manifest d'une indexation (knowledge-base.md §2, §9 ; ADR 0010 §3).

Ensemble des couples ``(point_id, chunk_hash)`` attendus dans l'index pour une
version. ``digest`` est l'empreinte conservée dans l'``Indexation`` : SHA-256
de la sérialisation canonique, une ligne ``"<point_id>:<chunk_hash>\\n"`` par
couple, dans l'ordre de ``str(point_id)``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Self
from uuid import UUID

from knowledge_base.domain.services.point_id import point_id
from knowledge_base.domain.value_objects.document_chunk import DocumentChunk

ManifestEntry = tuple[UUID, str]


@dataclass(frozen=True)
class IndexManifest:
    """``entries`` est trié par ``str(point_id)`` et sans doublon : construire
    par ``of`` ou ``for_chunks``."""

    entries: tuple[ManifestEntry, ...]

    def __post_init__(self) -> None:
        keys = [str(point) for point, _ in self.entries]
        if len(set(keys)) != len(keys):
            raise ValueError("point en double dans le manifest")
        if keys != sorted(keys):
            raise ValueError("manifest non trié par point")

    @classmethod
    def of(cls, entries: Iterable[ManifestEntry]) -> Self:
        return cls(tuple(sorted(entries, key=lambda entry: str(entry[0]))))

    @classmethod
    def for_chunks(cls, index_id: UUID, chunks: Iterable[DocumentChunk]) -> Self:
        return cls.of((point_id(index_id, c.chunk_id), c.chunk_hash) for c in chunks)

    @property
    def digest(self) -> str:
        canonical = "".join(
            f"{point}:{chunk_hash}\n" for point, chunk_hash in self.entries
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def __len__(self) -> int:
        return len(self.entries)
