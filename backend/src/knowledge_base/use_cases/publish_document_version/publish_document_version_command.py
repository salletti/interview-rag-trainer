from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class PublishDocumentVersion:
    document_id: UUID
    version_id: UUID
