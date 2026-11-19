"""Référence stable vers l'original (knowledge-base.md §2, §14 ; I13).

Elle désigne une version, une section et des pages, jamais un ``chunk_id`` :
elle reste valable après un rechunking (§13).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from knowledge_base.domain.value_objects.page_span import validate_pages
from knowledge_base.domain.value_objects.section_path import SectionPath


@dataclass(frozen=True)
class Citation:
    document_id: UUID
    document_title: str
    version_id: UUID
    version_number: int
    section_path: SectionPath
    page_start: int | None
    page_end: int | None
    source_filename: str

    def __post_init__(self) -> None:
        if not self.document_title.strip():
            raise ValueError("citation incomplète : titre du document vide")
        if not self.source_filename.strip():
            raise ValueError("citation incomplète : nom de fichier vide")
        if self.version_number < 1:
            raise ValueError(
                f"citation incomplète : numéro de version {self.version_number} < 1"
            )
        validate_pages(self.page_start, self.page_end)
