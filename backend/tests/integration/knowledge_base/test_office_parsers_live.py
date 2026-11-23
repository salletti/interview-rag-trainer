"""Parseurs PDF et DOCX de S6 avec le VRAI Docling (pipeline de layout, poids
de modèles téléchargés au premier lancement) : pas de service externe, mais
rattachés à la suite d'intégration (``model``) parce qu'ils chargent des modèles
(extras ``format-pdf``, ``format-docx``, ``models-local``, ``convert-core`` et
``opencv-python-headless``, hors lock au moment de S6 ; voir le rapport).

Trois rôles :

- les sorties ENREGISTRÉES (``fixtures/.../office/recorded/``) sur lesquelles
  s'appuient les tests unitaires de traduction sont bien celles que produit le
  parseur vivant (sinon l'enregistrement serait périmé) ;
- la limite de pages s'applique à ``max_pages`` pages INCLUSES (seule la
  page ``max_pages + 1`` est refusée) et un PDF sans couche texte donne
  ``OCR_REQUIRED`` ;
- les MESURES de coût d'extraction par page (CPU, RAM), consignées dans
  ``$KB_EXTRACTION_REPORT_DIR/office_extraction_report.json`` (défaut :
  ``backend/.cache/extraction/``, ignoré par git) : critère d'acceptation de S6
  (« mesures consignées ») et base de la révision des limites de l'ADR 0012.
"""

from __future__ import annotations

import json
import os
import re
import resource
import sys
import time
from pathlib import Path

import pytest

from knowledge_base.adapters.gateway.parsing.docling_docx_parser import (
    DoclingDocxParser,
)
from knowledge_base.adapters.gateway.parsing.docling_mapping import (
    structured_document_from_docling,
)
from knowledge_base.adapters.gateway.parsing.docling_pdf_parser import (
    DoclingPdfParser,
)
from knowledge_base.domain.exceptions.parsing_errors import (
    DocumentParseError,
    ParseErrorCode,
)
from knowledge_base.domain.value_objects.structured_document import Block, render_block
from tests.office_support import DOCX, PDF, office_bytes, recorded_docling

pytestmark = [pytest.mark.integration, pytest.mark.model]

BACKEND = Path(__file__).resolve().parents[3]
PAGES = {"qa_pytest_fr.pdf": 2, "cours_git_fr.pdf": 1, "pages.pdf": 3, "tableau.pdf": 1}
# Plafond de plausibilité (pas une limite produit) : une page de ce corpus qui
# coûterait plus de 30 s de CPU signalerait un pipeline cassé ou une
# configuration hors CPU nominal. Les vraies limites se décident sur la mesure.
CPU_SECONDS_PER_PAGE_CEILING = 30.0


def report_dir() -> Path:
    return Path(
        os.environ.get("KB_EXTRACTION_REPORT_DIR", BACKEND / ".cache" / "extraction")
    )


def squashed(blocks: tuple[Block, ...]) -> list[tuple[str, int | None, str]]:
    return [
        (type(b).__name__, b.page, re.sub(r"\s+", " ", render_block(b)).strip())
        for b in blocks
    ]


@pytest.mark.parametrize("name", sorted(PAGES))
def test_the_live_pdf_parser_gives_what_was_recorded(name: str) -> None:
    live = DoclingPdfParser().parse(office_bytes(name), PDF, name)

    recorded = structured_document_from_docling(recorded_docling(name))
    assert squashed(live.blocks) == squashed(recorded.blocks)


@pytest.mark.parametrize(
    "name", ["qa_pytest_fr.docx", "cours_git_fr.docx", "tableau.docx"]
)
def test_the_live_docx_parser_gives_what_was_recorded(name: str) -> None:
    live = DoclingDocxParser().parse(office_bytes(name), DOCX, name)

    recorded = structured_document_from_docling(recorded_docling(name))
    assert live.blocks == recorded.blocks
    assert dict(live.metadata) == {}


def test_a_pdf_with_exactly_max_pages_pages_is_extracted() -> None:
    document = DoclingPdfParser(max_pages=3).parse(
        office_bytes("pages.pdf"), PDF, "pages.pdf"
    )

    assert {b.page for b in document.blocks} == {1, 2, 3}


def test_a_pdf_with_one_page_more_than_max_pages_is_refused() -> None:
    with pytest.raises(DocumentParseError) as raised:
        DoclingPdfParser(max_pages=2).parse(office_bytes("pages.pdf"), PDF, "pages.pdf")

    assert raised.value.code == ParseErrorCode.TOO_MANY_PAGES


def test_a_pdf_without_a_text_layer_requires_ocr_instead_of_being_empty() -> None:
    with pytest.raises(DocumentParseError) as raised:
        DoclingPdfParser().parse(office_bytes("scanned.pdf"), PDF, "scan.pdf")

    assert raised.value.code == ParseErrorCode.OCR_REQUIRED
    assert "scan.pdf" in raised.value.message


def peak_rss_mib() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024 if sys.platform == "darwin" else 1024)


def test_the_extraction_cost_per_page_is_measured_and_recorded() -> None:
    """Ce parseur-ci est neuf : sa première conversion charge les modèles de
    layout et de tableaux (coût « à froid », sauf si un test précédent du même
    processus les a déjà chargés dans la même instance de bibliothèque) ; les
    suivantes sont « à chaud ». Le CPU est celui du processus (tous threads) ;
    la RAM, le pic de RSS du processus de test (il ne redescend jamais : une
    différence entre deux mesures est la croissance)."""
    parser = DoclingPdfParser()
    measures: dict[str, dict[str, float]] = {}
    for name in ("qa_pytest_fr.pdf", "pages.pdf", "cours_git_fr.pdf", "tableau.pdf"):
        content = office_bytes(name)
        rss_before = peak_rss_mib()
        cpu = time.process_time()
        wall = time.perf_counter()
        parser.parse(content, PDF, name)
        pages = PAGES[name]
        measures[name] = {
            "pages": pages,
            "wall_seconds": round(time.perf_counter() - wall, 3),
            "cpu_seconds": round(time.process_time() - cpu, 3),
            "cpu_seconds_per_page": round((time.process_time() - cpu) / pages, 3),
            "wall_seconds_per_page": round((time.perf_counter() - wall) / pages, 3),
            "peak_rss_mib_before": round(rss_before, 1),
            "peak_rss_mib_after": round(peak_rss_mib(), 1),
        }
    first = next(iter(measures))
    report = {
        "pipeline": "Docling PDF, OCR désactivé, structure des tableaux activée",
        "platform": sys.platform,
        "first_measured_document": first,
        "per_document": measures,
    }
    directory = report_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "office_extraction_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    recorded = json.loads(path.read_text(encoding="utf-8"))
    assert set(recorded["per_document"]) == set(PAGES)
    warm = [m for n, m in measures.items() if n != first]
    assert warm
    assert all(m["cpu_seconds_per_page"] < CPU_SECONDS_PER_PAGE_CEILING for m in warm)
    assert all(m["peak_rss_mib_after"] > 0 for m in measures.values())
