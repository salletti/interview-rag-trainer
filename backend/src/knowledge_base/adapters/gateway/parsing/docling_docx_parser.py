"""DOCX (Word OOXML) -> ``StructuredDocument`` par Docling (ADR 0008 §2 ;
roadmap S6). Aucun modèle : le backend Word de Docling lit le XML du document.

- Titres : le style « Titre » donne le niveau 1, « Titre n » le niveau n + 1
  (``docling_mapping``), comme les ``#`` d'un Markdown.
- Un DOCX n'a pas de pagination avant sa mise en page : ``page`` vaut ``None``
  et aucune limite de pages ne s'applique (la limite d'octets de l'import,
  si : ADR 0012).
- Contenu illisible (pas un ZIP, ZIP sans document Word, Word binaire ``.doc``,
  document chiffré, qui est un conteneur OLE) : ``INVALID_DOCUMENT``.

Docling est importé à la première conversion.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import PurePath
from typing import TYPE_CHECKING

from knowledge_base.adapters.gateway.parsing.docling_mapping import (
    structured_document_from_docling,
)
from knowledge_base.domain.exceptions.parsing_errors import (
    EMPTY_DOCUMENT,
    INVALID_DOCUMENT,
    DocumentParseError,
)
from knowledge_base.domain.value_objects.structured_document import (
    StructuredDocument,
)

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter
    from docling_core.types.doc import DoclingDocument


class DoclingDocxParser:
    def __init__(self) -> None:
        self._converter: DocumentConverter | None = None

    def parse(
        self, content: bytes, media_type: str, filename: str
    ) -> StructuredDocument:
        blocks = structured_document_from_docling(
            self._convert(content, filename)
        ).blocks
        if not blocks:
            raise DocumentParseError(
                EMPTY_DOCUMENT, f"{filename} ne contient aucun bloc exploitable"
            )
        return StructuredDocument(blocks=blocks)

    def _convert(self, content: bytes, filename: str) -> DoclingDocument:
        from docling.datamodel.base_models import DocumentStream, InputFormat
        from docling.document_converter import DocumentConverter
        from docling.exceptions import ConversionError

        if self._converter is None:
            self._converter = DocumentConverter(allowed_formats=[InputFormat.DOCX])
        # Docling choisit le format sur l'extension : le nom transmis est
        # toujours « .docx », quel que soit celui de l'import.
        stream = DocumentStream(
            name=f"{PurePath(filename).stem or 'document'}.docx",
            stream=BytesIO(content),
        )
        try:
            return self._converter.convert(stream).document
        except ConversionError as error:
            raise DocumentParseError(
                INVALID_DOCUMENT,
                f"{filename} : document Word illisible, chiffré ou corrompu ({error})",
            ) from error
