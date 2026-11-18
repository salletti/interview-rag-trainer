"""Extraction PDF et DOCX de bout en bout (S6 ; PostgreSQL réel, volume de
blobs temporaire, vrai Docling) : import par l'API, puis ``extract_version``
exécuté par ``execute_work_item`` avec le parseur de PRODUCTION
(``default_parser()``), compteur de mots et politique ``itest-1`` (120 tokens).

Prérequis hors lock (voir le rapport de S6) : extras Docling ``format-pdf``,
``format-docx`` et ``models-local`` (poids de layout et de TableFormer,
téléchargés au premier lancement), ``opencv-python-headless`` et
``convert-core`` (``rtree``). Les tests qui lancent le pipeline de layout sont
marqués ``model``.

Équivalence (roadmap S6) : un PDF ou un DOCX qui reproduit un Markdown du jeu de
référence donne les MÊMES unités que ce Markdown : même nombre, mêmes types,
mêmes questions, même texte (aux espaces, aux barrières de code et aux ``#``
près : Docling PDF aplatit le code). Le chemin de sections d'un DOCX est
identique ; celui d'un PDF perd sa racine, car Docling n'infère aucune
hiérarchie de titres dans un PDF : seul le dernier élément est comparé.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from knowledge_base.adapters.controller.tasks.extract_version_handler import (
    build_extract_version_handler,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    SqlAlchemyWorkLifecycle,
)
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.extraction_support import (
    POLICY,
    ExtractionRig,
    open_extraction_rig,
)
from tests.integration.knowledge_base.support import kb_settings
from tests.office_support import DOCX, PDF, RETRIEVAL, minimal_pdf, office_bytes
from tests.unit.knowledge_base.domain.services.support import COUNTER

pytestmark = pytest.mark.integration

MARKDOWN = "text/markdown"
MEDIA_TYPES = {"pdf": PDF, "docx": DOCX}


@pytest.fixture
async def rig(
    integration_settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[ExtractionRig]:
    async with open_extraction_rig(
        integration_settings, session_factory, tmp_path, monkeypatch
    ) as opened:
        yield opened


async def extract(
    rig: ExtractionRig,
    content: bytes,
    *,
    filename: str,
    media_type: str,
    kind: str = "COURSE",
) -> tuple[str, Any]:
    """Importe puis extrait avec le parseur de production ; rend l'issue de
    l'item et l'import."""
    imported = await rig.import_document(
        content, kind=kind, filename=filename, media_type=media_type
    )
    handler = rig.handler_with(None)  # None : default_parser() de production
    return await rig.run(imported.item_id, handler), imported


def words(text: str) -> str:
    """Texte sans barrières de code, sans ``#`` de titre ni accents graves, aux
    espaces normalisés : ce qui reste quand seul le format change."""
    text = re.sub(r"^```.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^#+\s+", "", text, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", text.replace("`", "")).strip()


def comparable(units: Sequence[dict[str, Any]], *, full_path: bool) -> list[Any]:
    return [
        (
            unit["unit_type"],
            unit["question_text"],
            tuple(unit["section_path"])
            if full_path
            else tuple(unit["section_path"][-1:])
            if unit["unit_type"] == "SECTION"
            else (),
            words(unit["text"]),
        )
        for unit in units
    ]


# --- équivalence avec le Markdown -----------------------------------------------


@pytest.mark.model
@pytest.mark.parametrize("extension", ["pdf", "docx"])
@pytest.mark.parametrize(
    ("name", "kind"), [("qa_pytest_fr", "QA_SHEET"), ("cours_git_fr", "COURSE")]
)
async def test_a_pdf_or_docx_gives_the_units_of_the_equivalent_markdown(
    rig: ExtractionRig, name: str, kind: str, extension: str
) -> None:
    markdown_outcome, markdown = await extract(
        rig,
        (RETRIEVAL / f"{name}.md").read_bytes(),
        filename=f"{name}.md",
        media_type=MARKDOWN,
        kind=kind,
    )
    outcome, office = await extract(
        rig,
        office_bytes(f"{name}.{extension}"),
        filename=f"{name}.{extension}",
        media_type=MEDIA_TYPES[extension],
        kind=kind,
    )

    assert (markdown_outcome, outcome) == ("succeeded", "succeeded")
    expected = await rig.units(markdown.version_id)
    got = await rig.units(office.version_id)
    assert len(expected) == len(got) == 6
    assert comparable(got, full_path=extension == "docx") == comparable(
        expected, full_path=extension == "docx"
    )
    assert (await rig.version(office.version_id))["status"] == "EXTRACTED"


@pytest.mark.model
async def test_pdf_units_carry_page_start_and_page_end_and_docx_units_none(
    rig: ExtractionRig,
) -> None:
    _, pdf = await extract(
        rig, office_bytes("pages.pdf"), filename="pages.pdf", media_type=PDF
    )
    _, docx = await extract(
        rig, office_bytes("cours_git_fr.docx"), filename="git.docx", media_type=DOCX
    )

    pdf_units = await rig.units(pdf.version_id)
    assert [
        (u["section_path"][-1], u["page_start"], u["page_end"]) for u in pdf_units
    ] == [
        ("Première section", 1, 1),
        ("Deuxième section", 2, 3),
        ("Troisième section", 3, 3),
    ]
    assert {
        (u["page_start"], u["page_end"]) for u in await rig.units(docx.version_id)
    } == {(None, None)}


@pytest.mark.model
@pytest.mark.parametrize(
    ("name", "media_type"), [("tableau.pdf", PDF), ("tableau.docx", DOCX)]
)
async def test_a_table_is_kept_row_by_row_in_the_unit_text(
    rig: ExtractionRig, name: str, media_type: str
) -> None:
    outcome, imported = await extract(
        rig, office_bytes(name), filename=name, media_type=media_type
    )

    assert outcome == "succeeded"
    (unit,) = await rig.units(imported.version_id)
    assert "| Méthode | Idempotente | Sûre |" in unit["text"]
    assert "| PUT | oui | non |" in unit["text"]


# --- échecs terminaux --------------------------------------------------------------


async def assert_failed_terminally(
    rig: ExtractionRig, outcome: str, imported: Any, code: str
) -> None:
    assert outcome == "failed"
    item = await rig.item(imported.item_id)
    assert item["status"] == "FAILED"
    assert item["attempts"] == 1  # aucun retry
    assert item["last_error_code"] == code
    version = await rig.version(imported.version_id)
    assert version["status"] == "FAILED"
    assert version["failure_code"] == code
    assert version["failure_message"]
    assert version["failure_retryable"] is False
    assert await rig.counts() == (0, 0)
    assert await rig.run(imported.item_id) == "skipped"  # jamais rejoué


@pytest.mark.model
async def test_a_scanned_pdf_fails_with_ocr_required_and_is_not_retried(
    rig: ExtractionRig,
) -> None:
    outcome, imported = await extract(
        rig, office_bytes("scanned.pdf"), filename="scan.pdf", media_type=PDF
    )

    await assert_failed_terminally(rig, outcome, imported, "OCR_REQUIRED")


@pytest.mark.parametrize(
    ("content", "filename", "media_type"),
    [
        (office_bytes("encrypted.pdf"), "chiffre.pdf", PDF),
        (office_bytes("corrupted.pdf"), "corrompu.pdf", PDF),
        (b"PK\x03\x04pas un docx", "corrompu.docx", DOCX),
    ],
    ids=["encrypted-pdf", "corrupted-pdf", "corrupted-docx"],
)
async def test_an_encrypted_or_corrupted_file_fails_terminally_as_invalid_document(
    rig: ExtractionRig, content: bytes, filename: str, media_type: str
) -> None:
    outcome, imported = await extract(
        rig, content, filename=filename, media_type=media_type
    )

    await assert_failed_terminally(rig, outcome, imported, "INVALID_DOCUMENT")


async def test_a_pdf_of_more_than_300_pages_is_refused_without_retry(
    rig: ExtractionRig,
) -> None:
    content = minimal_pdf(301)
    assert len(content) < 64 * 1024  # sous la limite d'envoi du rig d'import

    outcome, imported = await extract(
        rig, content, filename="trop-long.pdf", media_type=PDF
    )

    await assert_failed_terminally(rig, outcome, imported, "TOO_MANY_PAGES")
    message = (await rig.version(imported.version_id))["failure_message"]
    assert "301" in message


# --- heartbeats ---------------------------------------------------------------------


@pytest.mark.model
@pytest.mark.parametrize(
    ("name", "media_type"), [("cours_git_fr.pdf", PDF), ("pages.pdf", PDF)]
)
async def test_heartbeats_are_sent_while_the_pdf_is_being_extracted(
    rig: ExtractionRig,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    media_type: str,
) -> None:
    """Le travail CPU de Docling tourne dans un thread : la boucle reste libre
    et prolonge le lease pendant l'extraction. Si le parseur bloquait la boucle,
    aucun heartbeat ne partirait avant la fin."""
    beats: list[float] = []
    real_heartbeat = SqlAlchemyWorkLifecycle.heartbeat

    async def counting(self: SqlAlchemyWorkLifecycle, *args: Any) -> bool:
        beats.append(1.0)
        return await real_heartbeat(self, *args)

    monkeypatch.setattr(SqlAlchemyWorkLifecycle, "heartbeat", counting)
    settings = kb_settings(
        work_backoff_base_seconds=0.01, work_heartbeat_interval_seconds=0.01
    )
    assert settings.heartbeat_interval == timedelta(seconds=0.01)
    imported = await rig.import_document(
        office_bytes(name), filename=name, media_type=media_type
    )
    handler = build_extract_version_handler(
        rig.session_factory, settings, token_counter=COUNTER, policy=POLICY
    )

    outcome = await rig.run(imported.item_id, handler, settings)

    assert outcome == "succeeded"
    assert len(beats) >= 2
