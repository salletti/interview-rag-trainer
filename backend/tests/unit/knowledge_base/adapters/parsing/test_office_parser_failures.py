"""Échecs déterministes des parseurs PDF et DOCX de S6, sans modèle de layout
(roadmap S6 ; ADR 0008, point 5 ; ADR 0012, point 4).

API supposée : ``DoclingPdfParser(*, max_pages: int = MAX_PDF_PAGES)`` dans
``adapters/gateway/parsing/docling_pdf_parser.py`` (``MAX_PDF_PAGES = 300``) et
``DoclingDocxParser()`` dans ``docling_docx_parser.py`` ; l'un et l'autre sont
des ``DocumentParser`` (``parse(content, media_type, filename)``). Aucun type
Docling ne sort : tout échec est une ``DocumentParseError``.

Ces échecs sont détectés AVANT le pipeline de layout (qui charge des modèles
et coûte des secondes par page) : ces tests tournent sans poids de modèle et
doivent rester rapides. Ils s'appuient sur les extras Docling ``format-pdf``
(pypdfium2 : comptage des pages, détection du chiffrement) et ``format-docx``
(backend Word), dépendances du projet.

- PDF chiffré ou corrompu, DOCX illisible : ``INVALID_DOCUMENT``.
- Plus de ``max_pages`` pages : ``TOO_MANY_PAGES`` (le message donne le nombre
  de pages du document et la limite). La limite ne s'applique qu'au PDF : un
  DOCX n'a pas de pagination avant sa mise en page.
- DOCX dont l'archive entière déclare plus de ``max_uncompressed_bytes``
  décompressés (``MAX_DOCX_UNCOMPRESSED_BYTES``, 100 Mio ; revue S6, I2 /
  O16 ; étendu de ``word/*.xml`` à toutes les parties par la review
  indépendante, constat 4) : ``DOCUMENT_TOO_COMPLEX`` avant Docling, message
  avec taille et limite.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from docling_core.types.doc import DocItemLabel, DoclingDocument

from knowledge_base.adapters.gateway.parsing.docling_conversion import (
    DoclingConversion,
)
from knowledge_base.adapters.gateway.parsing.docling_docx_parser import (
    MAX_DOCX_UNCOMPRESSED_BYTES,
    DoclingDocxParser,
)
from knowledge_base.adapters.gateway.parsing.docling_pdf_parser import (
    MAX_PDF_PAGES,
    DoclingPdfParser,
)
from knowledge_base.domain.exceptions.parsing_errors import (
    DOCUMENT_TOO_COMPLEX,
    INVALID_DOCUMENT,
    DocumentParseError,
    ParseErrorCode,
)
from tests.office_support import DOCX, PDF, minimal_pdf, office_bytes


def test_the_default_page_limit_is_the_300_pages_of_adr_0012() -> None:
    assert MAX_PDF_PAGES == 300


def test_a_pdf_over_the_default_limit_is_refused_with_its_page_count() -> None:
    with pytest.raises(DocumentParseError) as raised:
        DoclingPdfParser().parse(minimal_pdf(301), PDF, "long.pdf")

    assert raised.value.code == ParseErrorCode.TOO_MANY_PAGES
    assert "301" in raised.value.message
    assert "300" in raised.value.message


def test_the_page_limit_is_configurable_and_exclusive_of_the_limit_itself() -> None:
    parser = DoclingPdfParser(max_pages=2)

    with pytest.raises(DocumentParseError) as raised:
        parser.parse(minimal_pdf(3), PDF, "trois-pages.pdf")

    assert raised.value.code == ParseErrorCode.TOO_MANY_PAGES


@pytest.mark.parametrize(
    "name", ["encrypted.pdf", "corrupted.pdf"], ids=["encrypted", "corrupted"]
)
def test_an_encrypted_or_corrupted_pdf_is_a_terminal_invalid_document(
    name: str,
) -> None:
    with pytest.raises(DocumentParseError) as raised:
        DoclingPdfParser().parse(office_bytes(name), PDF, name)

    assert raised.value.code == INVALID_DOCUMENT
    assert name in raised.value.message


@pytest.mark.parametrize(
    "content",
    [b"", b"ceci n'est pas un pdf", b"%PDF-1.7\n"],
    ids=["empty", "text", "header"],
)
def test_bytes_that_are_not_a_pdf_are_a_terminal_invalid_document(
    content: bytes,
) -> None:
    with pytest.raises(DocumentParseError) as raised:
        DoclingPdfParser().parse(content, PDF, "faux.pdf")

    assert raised.value.code == INVALID_DOCUMENT


def not_a_word_document() -> bytes:
    """Un ZIP valide qui n'est pas un document Word (pas de ``word/``)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("lisez-moi.txt", "pas du OOXML")
    return buffer.getvalue()


@pytest.mark.parametrize(
    "content",
    [b"", b"ceci n'est pas un docx", b"PK\x03\x04tronque", not_a_word_document()],
    ids=["empty", "text", "truncated-zip", "zip-without-word"],
)
def test_bytes_that_are_not_a_docx_are_a_terminal_invalid_document(
    content: bytes,
) -> None:
    with pytest.raises(DocumentParseError) as raised:
        DoclingDocxParser().parse(content, DOCX, "faux.docx")

    assert raised.value.code == INVALID_DOCUMENT


@pytest.fixture
def conversions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Noms des fichiers réellement confiés à Docling (double)."""
    converted: list[str] = []

    def convert(self: DoclingConversion, content: bytes, filename: str) -> object:
        converted.append(filename)
        document = DoclingDocument(name="double")
        document.add_text(label=DocItemLabel.TEXT, text="Texte converti.")
        return document

    monkeypatch.setattr(DoclingConversion, "convert", convert)
    return converted


def word_archive(parts: dict[str, int]) -> bytes:
    """ZIP en mémoire : chaque partie est ``taille`` octets d'espaces,
    compressés (la taille décompressée déclarée est ``taille``)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, size in parts.items():
            archive.writestr(name, b" " * size)
    return buffer.getvalue()


def test_the_default_docx_uncompressed_limit_is_100_mib() -> None:
    assert MAX_DOCX_UNCOMPRESSED_BYTES == 100 * 1024 * 1024


def test_a_docx_declaring_more_than_the_limit_is_too_complex(
    conversions: list[str],
) -> None:
    content = word_archive({"word/document.xml": 600, "word/styles.xml": 500})

    with pytest.raises(DocumentParseError) as raised:
        DoclingDocxParser(max_uncompressed_bytes=1000).parse(content, DOCX, "gros.docx")

    assert raised.value.code == DOCUMENT_TOO_COMPLEX
    assert "gros.docx" in raised.value.message
    assert "1100 octets" in raised.value.message  # somme des parties
    assert "limite" in raised.value.message
    assert conversions == []


def test_a_docx_at_the_limit_is_converted(conversions: list[str]) -> None:
    parser = DoclingDocxParser(max_uncompressed_bytes=1000)

    parser.parse(
        word_archive({"word/document.xml": 600, "word/media/image1.png": 400}),
        DOCX,
        "limite.docx",
    )

    assert conversions == ["limite.docx"]


@pytest.mark.parametrize(
    "part",
    ["word/media/image1.png", "customXml/item1.xml", "word/_rels/document.xml.rels"],
)
def test_every_part_of_the_archive_counts_not_only_the_word_xml(
    part: str, conversions: list[str]
) -> None:
    content = word_archive({"word/document.xml": 10, part: 5000})

    with pytest.raises(DocumentParseError) as raised:
        DoclingDocxParser(max_uncompressed_bytes=1000).parse(
            content, DOCX, "parties.docx"
        )

    assert raised.value.code == DOCUMENT_TOO_COMPLEX
    assert "5010 octets" in raised.value.message
    assert conversions == []


def test_a_docx_over_the_default_limit_is_refused_before_docling(
    conversions: list[str],
) -> None:
    """101 Mio d'XML décompressé, ~100 Kio compressés : une bombe de
    décompression ordinaire, sous la limite d'octets de l'import (20 Mio)."""
    buffer = io.BytesIO()
    with (
        zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive,
        archive.open("word/document.xml", "w") as part,
    ):
        for _ in range(101):
            part.write(b" " * (1024 * 1024))

    with pytest.raises(DocumentParseError) as raised:
        DoclingDocxParser().parse(buffer.getvalue(), DOCX, "bombe.docx")

    assert raised.value.code == DOCUMENT_TOO_COMPLEX
    assert "101.0 Mio" in raised.value.message
    assert "100.0 Mio" in raised.value.message
    assert conversions == []


@pytest.mark.parametrize(
    "content",
    [b"", b"ceci n'est pas un docx", b"PK\x03\x04tronque"],
    ids=["empty", "text", "truncated-zip"],
)
def test_an_unreadable_zip_is_invalid_before_docling(
    content: bytes, conversions: list[str]
) -> None:
    with pytest.raises(DocumentParseError) as raised:
        DoclingDocxParser().parse(content, DOCX, "faux.docx")

    assert raised.value.code == INVALID_DOCUMENT
    assert "faux.docx" in raised.value.message
    assert conversions == []


# --- parties reliées hors XML Word (review indépendante, constat 4) -------------
#
# python-docx, que Docling utilise, charge toute partie atteinte par les
# relations OPC, binaires comprises : borner le seul XML de ``word/`` laissait
# une partie reliée très compressible passer avant Docling.

PACKAGE_RELATIONSHIP = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/package"
)
_MIB = 1024 * 1024


def with_related_binary_part(base: bytes, size: int) -> bytes:
    """``base`` (un vrai DOCX) enrichi de ``word/embeddings/data.bin``, ``size``
    octets nuls compressés, relié au document principal par une relation
    OPC (et déclaré dans ``[Content_Types].xml``)."""
    buffer = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(base)) as source,
        zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            data = source.read(info)
            if info.filename == "word/_rels/document.xml.rels":
                data = data.replace(
                    b"</Relationships>",
                    f'<Relationship Id="rIdBin" Type="{PACKAGE_RELATIONSHIP}" '
                    'Target="embeddings/data.bin"/></Relationships>'.encode(),
                )
            elif info.filename == "[Content_Types].xml":
                data = data.replace(
                    b"</Types>",
                    b'<Default Extension="bin" ContentType="application/'
                    b'vnd.openxmlformats-officedocument.oleObject"/></Types>',
                )
            target.writestr(info.filename, data)
        with target.open("word/embeddings/data.bin", "w") as part:
            remaining = size
            while remaining:
                piece = min(remaining, _MIB)
                part.write(b"\0" * piece)
                remaining -= piece
    return buffer.getvalue()


def test_the_related_binary_part_is_really_loaded_by_python_docx() -> None:
    """Le scénario est réaliste : le chargeur de python-docx lit la partie."""
    from docx.opc.pkgreader import PackageReader

    content = with_related_binary_part(office_bytes("cours_git_fr.docx"), 4096)

    # python-docx n'est pas typé : son chargeur est appelé tel quel.
    reader = PackageReader.from_file(io.BytesIO(content))  # type: ignore[no-untyped-call]

    loaded = {str(name): len(blob) for name, _, _, blob in reader.iter_sparts()}
    assert loaded["/word/embeddings/data.bin"] == 4096


def test_a_related_binary_part_over_the_default_limit_is_refused_before_docling(
    conversions: list[str],
) -> None:
    """XML Word de quelques centaines de Kio, partie binaire reliée de
    100 Mio + 1 octet : l'archive tient en ~100 Kio."""
    content = with_related_binary_part(
        office_bytes("cours_git_fr.docx"), 100 * _MIB + 1
    )
    assert len(content) < _MIB

    with pytest.raises(DocumentParseError) as raised:
        DoclingDocxParser().parse(content, DOCX, "binaire.docx")

    assert raised.value.code == DOCUMENT_TOO_COMPLEX
    assert "binaire.docx" in raised.value.message
    assert "100.0 Mio" in raised.value.message
    assert conversions == []


@pytest.mark.parametrize(
    "name", ["cours_git_fr.docx", "qa_pytest_fr.docx", "tableau.docx"]
)
def test_the_docx_fixtures_stay_within_the_default_limit(
    name: str, conversions: list[str]
) -> None:
    DoclingDocxParser().parse(office_bytes(name), DOCX, name)

    assert conversions == [name]
