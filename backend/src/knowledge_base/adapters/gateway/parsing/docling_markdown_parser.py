"""Markdown -> ``StructuredDocument`` par Docling (ADR 0008 §2) : couche
anti-corruption, aucun type Docling ne sort de ce module.

- Le front-matter (``---`` / ``clé: valeur`` / ``---``) est lu ici, AVANT
  Docling, et rendu dans ``metadata`` ; Docling ne reçoit que le corps. Le
  bloc n'est un front-matter que si chacune de ses lignes est ``clé: valeur``,
  vide ou commentaire ``#`` : sinon (une règle horizontale suivie de prose,
  par exemple) le document est rendu tel quel à Docling. Les guillemets
  simples ou doubles qui entourent une valeur sont retirés.
- Traduction des blocs (titres, code, listes, tableaux, paragraphes à mise en
  forme) : ``docling_mapping``, commune à PDF et DOCX. Les retours à la ligne
  d'un paragraphe deviennent des espaces ; le langage d'un bloc de code est
  celui que Docling reconnaît (barrière ou détection). Markdown n'a pas de
  pages : ``page`` vaut toujours ``None``.
- Imbrication trop profonde (listes, citations) : Docling échoue sur une
  ``RecursionError`` ; l'échec est déterministe, rendu en
  ``DOCUMENT_TOO_COMPLEX``. Le parcours de l'arbre Docling, lui, est itératif.

Docling est importé à la première conversion, pas à l'import du module : l'API
compose le handler d'extraction sans payer le chargement de Docling.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from knowledge_base.adapters.gateway.parsing.docling_conversion import (
    DoclingConversion,
    document_with_blocks,
)
from knowledge_base.adapters.gateway.parsing.plain_text_parser import decode_utf8
from knowledge_base.domain.exceptions.parsing_errors import (
    DOCUMENT_TOO_COMPLEX,
    EMPTY_DOCUMENT,
    DocumentParseError,
)
from knowledge_base.domain.value_objects.structured_document import (
    StructuredDocument,
)

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter

FRONT_MATTER_FENCE: Final = "---"
_FRONT_MATTER_ENTRY: Final = re.compile(r"\s*(?P<key>[\w.-]+)\s*:(?P<value>.*)")
_QUOTES: Final = ('"', "'")


class DoclingMarkdownParser:
    def __init__(self) -> None:
        self._conversion = DoclingConversion(".md", _markdown_converter)

    def parse(
        self, content: bytes, media_type: str, filename: str
    ) -> StructuredDocument:
        text = decode_utf8(content, filename).replace("\r\n", "\n")
        metadata, body = read_front_matter(text)
        if not body.strip():
            raise DocumentParseError(
                EMPTY_DOCUMENT, f"{filename} ne contient aucun texte"
            )
        try:
            document = document_with_blocks(
                self._conversion.convert(body.encode(), filename), filename
            )
        except RecursionError as error:
            raise _too_complex(filename) from error
        except RuntimeError as error:
            # Le pipeline Docling enveloppe toute erreur de son backend dans
            # RuntimeError(« Pipeline … failed ») ; seule la récursion est
            # reconnue comme déterministe, le reste remonte tel quel.
            if isinstance(error.__cause__, RecursionError):
                raise _too_complex(filename) from error
            raise
        return StructuredDocument(blocks=document.blocks, metadata=metadata)


def _markdown_converter() -> DocumentConverter:
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter

    return DocumentConverter(allowed_formats=[InputFormat.MD])


def _too_complex(filename: str) -> DocumentParseError:
    return DocumentParseError(
        DOCUMENT_TOO_COMPLEX,
        f"{filename} : imbrication trop profonde pour être convertie",
    )


def read_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Métadonnées ``clé: valeur`` d'un front-matter en tête de document, et le
    reste du texte. Sans barrière fermante, ou si une ligne du bloc n'est ni
    ``clé: valeur``, ni vide, ni un commentaire, il n'y a pas de front-matter."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != FRONT_MATTER_FENCE:
        return {}, text
    for end in range(1, len(lines)):
        if lines[end].strip() == FRONT_MATTER_FENCE:
            metadata = _front_matter_entries(lines[1:end])
            if metadata is None:
                return {}, text
            return metadata, "\n".join(lines[end + 1 :])
    return {}, text


def _front_matter_entries(lines: list[str]) -> dict[str, str] | None:
    """Les entrées du bloc ; ``None`` dès qu'une ligne n'en est pas une."""
    metadata: dict[str, str] = {}
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        entry = _FRONT_MATTER_ENTRY.fullmatch(line)
        if entry is None:
            return None
        metadata[entry["key"]] = _unquoted(entry["value"].strip())
    return metadata


def _unquoted(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES:
        return value[1:-1]
    return value
