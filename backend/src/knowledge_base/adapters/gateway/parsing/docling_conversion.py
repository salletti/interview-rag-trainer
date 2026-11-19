"""Conversion d'un contenu par un ``DocumentConverter`` de Docling, commune aux
parseurs Markdown, PDF et DOCX.

- Le convertisseur est créé à la première conversion, pas à la construction :
  composer un parseur ne charge pas Docling.
- Un verrou par parseur couvre la création du convertisseur et chaque
  conversion : les conversions d'un même parseur sont sérialisées dans le
  processus. Quand ``keeping_lease`` abandonne une tentative (lease perdu), le
  thread qui convertit va à son terme ; la tâche suivante du worker attend
  qu'il ait fini au lieu de partager le convertisseur avec lui (Docling ne le
  garantit pas réentrant) ou d'en charger un second.
- Docling choisit le format sur l'extension du nom transmis : c'est toujours
  celle du format du parseur, quel que soit le nom de l'import.
- ``ConversionError`` -> ``INVALID_DOCUMENT``, avec l'erreur de Docling et sa
  cause première (``BadZipFile``, ``XMLSyntaxError``…) : le message de Docling
  seul ne dit pas pourquoi. Une erreur de syntaxe XML (dont la profondeur
  maximale de lxml) est signalée « structure invalide ou trop complexe ».
- Conversion partielle (``PARTIAL_SUCCESS``) : acceptée, son statut et ses
  erreurs sont journalisés.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
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

logger = logging.getLogger(__name__)


class DoclingConversion:
    def __init__(
        self, extension: str, build_converter: Callable[[], DocumentConverter]
    ) -> None:
        """``extension`` : celle qui fait reconnaître le format à Docling
        (``.pdf``) ; ``build_converter`` n'est appelé qu'une fois, à la
        première conversion."""
        self._extension = extension
        self._build_converter = build_converter
        self._converter: DocumentConverter | None = None
        self._lock = threading.Lock()

    def convert(self, content: bytes, filename: str) -> DoclingDocument:
        from docling.datamodel.base_models import ConversionStatus, DocumentStream
        from docling.exceptions import ConversionError

        stream = DocumentStream(
            name=f"{PurePath(filename).stem or 'document'}{self._extension}",
            stream=BytesIO(content),
        )
        try:
            with self._lock:
                if self._converter is None:
                    self._converter = self._build_converter()
                result = self._converter.convert(stream)
        except ConversionError as error:
            raise _invalid_document(filename, error) from error
        if result.status != ConversionStatus.SUCCESS:
            logger.warning(
                "%s : conversion Docling %s, erreurs : %s",
                filename,
                result.status.value,
                "; ".join(e.error_message for e in result.errors) or "aucune",
            )
        return result.document


def document_with_blocks(
    document: DoclingDocument, filename: str
) -> StructuredDocument:
    """Traduction du document Docling ; ``EMPTY_DOCUMENT`` s'il n'en reste
    aucun bloc."""
    structured = structured_document_from_docling(document)
    if not structured.blocks:
        raise DocumentParseError(
            EMPTY_DOCUMENT, f"{filename} ne contient aucun bloc exploitable"
        )
    return structured


def _invalid_document(filename: str, error: Exception) -> DocumentParseError:
    root = _root_cause(error)
    # lxml.etree.XMLSyntaxError hérite de SyntaxError : pas d'import de lxml.
    nature = (
        "structure invalide ou trop complexe"
        if isinstance(root, SyntaxError)
        else "document illisible ou corrompu"
    )
    cause = "" if root is error else f" ; cause : {type(root).__name__}: {root}"
    return DocumentParseError(
        INVALID_DOCUMENT,
        f"Docling n'a pas pu convertir {filename} ({nature}) : {error}{cause}",
    )


def _root_cause(error: BaseException) -> BaseException:
    seen = {id(error)}
    while error.__cause__ is not None and id(error.__cause__) not in seen:
        error = error.__cause__
        seen.add(id(error))
    return error
