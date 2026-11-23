"""Nature pédagogique d'un document (knowledge-base.md §2)."""

from __future__ import annotations

from enum import StrEnum


class DocumentKind(StrEnum):
    COURSE = "COURSE"
    QA_SHEET = "QA_SHEET"
    REVISION_SHEET = "REVISION_SHEET"
    TECHNICAL = "TECHNICAL"
