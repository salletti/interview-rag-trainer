"""Aides partagées des tests PDF/DOCX de la slice S6 (importées comme
``tests.office_support``) : types de média, chemins des fixtures de
``fixtures/knowledge_base/office/`` (générées par ``generate_office_fixtures.py``,
sorties de Docling enregistrées par ``record_docling_documents.py``) et un
générateur de PDF minimal, sans dépendance, pour les tests de limite de pages.

CONTRATS supposés de l'implémentation de S6 (voir le rapport de la slice) :

- types de média : ``application/pdf`` (extension ``.pdf``) et
  ``application/vnd.openxmlformats-officedocument.wordprocessingml.document``
  (extension ``.docx``) ;
- ``DoclingPdfParser`` et ``DoclingDocxParser`` (``adapters/gateway/parsing/``),
  chacun un ``DocumentParser`` ; ``DoclingPdfParser(max_pages=300)`` ;
- codes ``OCR_REQUIRED`` et ``TOO_MANY_PAGES`` dans ``ParseErrorCode`` ;
  PDF chiffré ou corrompu, DOCX illisible : ``INVALID_DOCUMENT``.
"""

from __future__ import annotations

import json
from pathlib import Path

from docling_core.types.doc import DoclingDocument

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "knowledge_base"
OFFICE = FIXTURES / "office"
RECORDED = OFFICE / "recorded"
RETRIEVAL = FIXTURES / "retrieval"


def office_bytes(name: str) -> bytes:
    return (OFFICE / name).read_bytes()


def recorded_docling(name: str) -> DoclingDocument:
    """Sortie réelle de Docling, enregistrée (``<fixture>.docling.json``)."""
    return DoclingDocument.model_validate(
        json.loads((RECORDED / f"{name}.docling.json").read_text(encoding="utf-8"))
    )


def minimal_pdf(pages: int) -> bytes:
    """PDF valide de ``pages`` pages (texte « Page n »), construit à la main :
    aucune bibliothèque, quelques centaines d'octets par page. Un seul flux de
    contenu est partagé par toutes les pages."""
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids ["
        + b" ".join(f"{5 + i} 0 R".encode() for i in range(pages))
        + b"] /Count "
        + str(pages).encode()
        + b" >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = b"BT /F1 14 Tf 72 720 Td (Une page de texte natif.) Tj ET"
    objects.append(
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream"
    )
    for _ in range(pages):
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Contents 4 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
        )
    body = b"%PDF-1.4\n"
    offsets: list[int] = []
    for number, content in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{number} 0 obj\n".encode() + content + b"\nendobj\n"
    xref = len(body)
    body += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        body += f"{offset:010d} 00000 n \n".encode()
    body += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return body
