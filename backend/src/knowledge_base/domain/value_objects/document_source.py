"""Provenance de l'original d'une version (knowledge-base.md §2, §3.5)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from knowledge_base.domain.value_objects.content_hash import ContentHash


@dataclass(frozen=True)
class DocumentSource:
    filename: str
    media_type: str
    size_bytes: int
    content_hash: ContentHash
    imported_at: datetime

    def __post_init__(self) -> None:
        if not self.filename.strip():
            raise ValueError("filename ne peut pas être vide")
        if self.size_bytes < 0:
            raise ValueError(f"size_bytes doit être positif ou nul : {self.size_bytes}")
