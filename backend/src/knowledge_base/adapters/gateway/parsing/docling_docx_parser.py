"""DOCX (Word OOXML) -> ``StructuredDocument`` par Docling (ADR 0008 §2 ;
roadmap S6). Aucun modèle : le backend Word de Docling lit le XML du document.

- Titres : le style « Titre » donne le niveau 1, « Titre n » le niveau n + 1
  (``docling_mapping``), comme les ``#`` d'un Markdown.
- Un DOCX n'a pas de pagination avant sa mise en page : ``page`` vaut ``None``
  et aucune limite de pages ne s'applique (la limite d'octets de l'import,
  si : ADR 0012).
- Avant Docling, la taille décompressée DÉCLARÉE de toute l'archive (somme
  des ``ZipInfo.file_size`` de toutes ses entrées) est bornée par
  ``max_uncompressed_bytes`` (``MAX_DOCX_UNCOMPRESSED_BYTES``, 100 Mio,
  Proposed) : au-delà, ``DOCUMENT_TOO_COMPLEX``. Les 20 Mio compressés de
  l'import peuvent contenir bien plus, que Docling chargerait en mémoire
  (O16). Toute l'archive, pas seulement ``word/*.xml`` (review indépendante,
  constat 4) : python-docx charge chaque partie atteinte par les relations
  OPC, binaires comprises ; borner l'archive entière couvre toute partie
  chargeable, sans dépendre du graphe des relations. La taille déclarée
  suffit : Docling lit l'archive par python-docx, donc par ``zipfile``, qui
  tronque toute partie à sa taille déclarée.
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

# Proposed (review indépendante, constat 4) : la valeur de l'ancienne borne du
# XML Word, étendue à toute l'archive ; non mesurée sur un DOCX réel de cette
# taille.
MAX_DOCX_UNCOMPRESSED_BYTES: Final = 100 * 1024 * 1024
_MIB: Final = 1024 * 1024


class DoclingDocxParser:
    def __init__(
        self, *, max_uncompressed_bytes: int = MAX_DOCX_UNCOMPRESSED_BYTES
    ) -> None:
        self._max_uncompressed_bytes = max_uncompressed_bytes
        self._conversion = DoclingConversion(".docx", _docx_converter)

    def parse(
        self, content: bytes, media_type: str, filename: str
    ) -> StructuredDocument:
        self._check_uncompressed_size(content, filename)
        return document_with_blocks(
            self._conversion.convert(content, filename), filename
        )

    def _check_uncompressed_size(self, content: bytes, filename: str) -> None:
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                uncompressed = sum(info.file_size for info in archive.infolist())
        except zipfile.BadZipFile as error:
            raise DocumentParseError(
                INVALID_DOCUMENT,
                f"{filename} : document Word illisible, ce n'est pas une archive "
                f"ZIP valide ({error})",
            ) from error
        if uncompressed > self._max_uncompressed_bytes:
            raise DocumentParseError(
                DOCUMENT_TOO_COMPLEX,
                f"{filename} : {uncompressed / _MIB:.1f} Mio décompressés "
                f"({uncompressed} octets, toutes parties de l'archive), au-delà "
                f"de la limite de {self._max_uncompressed_bytes / _MIB:.1f} Mio",
            )


def _docx_converter() -> DocumentConverter:
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter

    return DocumentConverter(allowed_formats=[InputFormat.DOCX])
