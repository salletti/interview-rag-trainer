"""Refus de ``RecordExtraction`` qui ne viennent pas du parseur (S8, O16)."""

from __future__ import annotations

from uuid import UUID

from knowledge_base.domain.value_objects.error_catalog import DOCUMENT_TOO_LARGE


class DocumentTooLargeError(Exception):
    """Le texte extrait dépasse la borne de volume commune à tous les formats
    (``KB_MAX_DOCUMENT_TOKENS``) : somme des ``token_count`` des unités,
    strictement au-dessus de la borne. Déterministe sur le contenu."""

    code = DOCUMENT_TOO_LARGE

    def __init__(self, version_id: UUID, tokens: int, limit: int) -> None:
        super().__init__(
            f"{DOCUMENT_TOO_LARGE}: version {version_id} : {tokens} tokens extraits, "
            f"au-delà de la borne de {limit} (KB_MAX_DOCUMENT_TOKENS)"
        )
        self.version_id = version_id
        self.tokens = tokens
        self.limit = limit
