"""DOCX (Word OOXML) -> ``StructuredDocument`` par Docling (ADR 0008 §2 ;
roadmap S6). Aucun modèle : le backend Word de Docling lit le XML du document.

- Titres : le style « Titre » donne le niveau 1, « Titre n » le niveau n + 1
  (``docling_mapping``), comme les ``#`` d'un Markdown.
- Un DOCX n'a pas de pagination avant sa mise en page : ``page`` vaut ``None``
  et aucune limite de pages ne s'applique (la limite d'octets de l'import,
  si : ADR 0012).
- Avant Docling, la taille décompressée DÉCLARÉE des parties ``word/*.xml``
  (somme des ``ZipInfo.file_size``) est bornée par ``max_xml_bytes``
  (``MAX_DOCX_XML_BYTES``, 100 Mio) : au-delà, ``DOCUMENT_TOO_COMPLEX``. Les
  20 Mio compressés de l'import peuvent contenir bien plus de XML, que
  Docling chargerait en mémoire (O16). La taille déclarée suffit : Docling lit
  l'archive par python-docx, donc par ``zipfile``, qui tronque toute partie à
  sa taille déclarée.
- Contenu illisible (pas un ZIP, ZIP sans document Word, Word binaire ``.doc``,
  document chiffré, qui est un conteneur OLE) : ``INVALID_DOCUMENT``.

Docling est importé à la première conversion.
"""

from __future__ import annotations

import zipfile
from io import BytesIO
from typing import TYPE_CHECKING, Final

from knowledge_base.adapters.gateway.parsing.docling_conversion import (
    DoclingConversion,
    document_with_blocks,
)
from knowledge_base.domain.exceptions.parsing_errors import (
    DOCUMENT_TOO_COMPLEX,
    INVALID_DOCUMENT,
    DocumentParseError,
)
from knowledge_base.domain.value_objects.structured_document import (
    StructuredDocument,
)

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter

MAX_DOCX_XML_BYTES: Final = 100 * 1024 * 1024
_MIB: Final = 1024 * 1024


class DoclingDocxParser:
    def __init__(self, *, max_xml_bytes: int = MAX_DOCX_XML_BYTES) -> None:
        self._max_xml_bytes = max_xml_bytes
        self._conversion = DoclingConversion(".docx", _docx_converter)

    def parse(
        self, content: bytes, media_type: str, filename: str
    ) -> StructuredDocument:
        self._check_xml_size(content, filename)
        return document_with_blocks(
            self._conversion.convert(content, filename), filename
        )

    def _check_xml_size(self, content: bytes, filename: str) -> None:
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                xml_bytes = sum(
                    info.file_size
                    for info in archive.infolist()
                    if info.filename.startswith("word/")
                    and info.filename.endswith(".xml")
                )
        except zipfile.BadZipFile as error:
            raise DocumentParseError(
                INVALID_DOCUMENT,
                f"{filename} : document Word illisible, ce n'est pas une archive "
                f"ZIP valide ({error})",
            ) from error
        if xml_bytes > self._max_xml_bytes:
            raise DocumentParseError(
                DOCUMENT_TOO_COMPLEX,
                f"{filename} : {xml_bytes / _MIB:.1f} Mio de XML Word décompressé "
                f"({xml_bytes} octets), au-delà de la limite de "
                f"{self._max_xml_bytes / _MIB:.1f} Mio",
            )


def _docx_converter() -> DocumentConverter:
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter

    return DocumentConverter(allowed_formats=[InputFormat.DOCX])
