from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class ImportDocumentResult:
    """``created`` : une version vient d'être créée (faux quand le contenu
    était déjà connu et que la version existante est rendue)."""

    document_id: UUID
    version_id: UUID
    number: int
    created: bool
