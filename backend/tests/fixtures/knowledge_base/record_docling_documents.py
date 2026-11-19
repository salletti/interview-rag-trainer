"""Enregistre les sorties RÉELLES de Docling (``DoclingDocument`` sérialisé en
JSON) des fixtures PDF et DOCX de ``office/`` : les tests unitaires de S6 en
rejouent la traduction vers ``StructuredDocument`` sans charger Docling ni
ses modèles.

Pas une dépendance du projet, pas collecté par pytest. Environnement éphémère,
versions épinglées (celles du lock pour ``docling-slim`` et ``docling-core`` ;
``opencv-python-headless`` est requis par TableFormer et absent des extras) :

    cd backend && uv run --no-project \\
        --with "docling-slim[format-pdf,format-docx,models-local,convert-core]\\
==2.150.1" \\
        --with docling-core==2.107.1 --with opencv-python-headless \\
        python tests/fixtures/knowledge_base/record_docling_documents.py

Pipeline PDF : OCR désactivé, structure des tableaux activée (roadmap S6),
modèle de layout à la révision épinglée par le parseur de production
(``LAYOUT_MODEL_REVISION`` de ``docling_pdf_parser``, recopiée ici : ce script
n'importe pas le projet). Les poids de layout et de TableFormer sont
téléchargés au premier lancement. Le test ``test_office_parsers_live`` vérifie
que le parseur de production rend ce qui est enregistré.
Sortie : ``office/recorded/<fixture>.docling.json``.

Si Docling ou ses modèles évoluent, régénérer ces fichiers est un changement
à relire : le diff montre ce que la nouvelle version extrait différemment.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import (
    LayoutObjectDetectionOptions,
    PdfPipelineOptions,
)
from docling.document_converter import DocumentConverter, PdfFormatOption

OFFICE = Path(__file__).resolve().parent / "office"
RECORDED = OFFICE / "recorded"
LAYOUT_MODEL_REVISION = "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8"
FIXTURES = (
    "qa_pytest_fr.pdf",
    "qa_pytest_fr.docx",
    "cours_git_fr.pdf",
    "cours_git_fr.docx",
    "tableau.pdf",
    "tableau.docx",
    "pages.pdf",
)


def main(names: list[str]) -> None:
    RECORDED.mkdir(exist_ok=True)
    layout = LayoutObjectDetectionOptions.from_preset("layout_heron_default")
    layout.model_spec = layout.model_spec.model_copy(
        update={"revision": LAYOUT_MODEL_REVISION}
    )
    options = PdfPipelineOptions(
        do_ocr=False, do_table_structure=True, layout_options=layout
    )
    converter = DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.DOCX],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
    )
    for name in names or FIXTURES:
        started = time.perf_counter()
        result = converter.convert(OFFICE / name)
        elapsed = time.perf_counter() - started
        target = RECORDED / f"{name}.docling.json"
        result.document.save_as_json(target)
        print(f"{name}: {result.status}, {elapsed:.2f} s -> {target.name}")  # noqa: T201
        for item, level in result.document.iterate_items():
            pages = [p.page_no for p in getattr(item, "prov", [])]
            text = getattr(item, "text", "")[:50]
            print(f"  {type(item).__name__:18} {level} p{pages} {text!r}")  # noqa: T201


if __name__ == "__main__":
    main(sys.argv[1:])
