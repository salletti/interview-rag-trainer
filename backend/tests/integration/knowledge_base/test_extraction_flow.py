"""Slice S3, bout en bout : fichier importé par l'API de S2, puis item
``extract_version`` exécuté par ``execute_work_item`` -> unités et chunks en
base, version ``EXTRACTED`` (PostgreSQL réel).

API figée (voir ``extraction_support``) : ``knowledge_units`` (``id``,
``version_id``, ``chunking_policy_version``, ``ordinal``, ``unit_type``,
``question_text``, ``section_path`` (tableau), ``page_start``, ``page_end``,
``token_count``, ``text``) et ``document_chunks`` (``id``, ``unit_id``,
``version_id``, ``chunking_policy_version``, ``ordinal_in_unit``,
``contextual_text``, ``chunk_hash``, ``token_count``, ``citation`` JSONB aux clés
de ``Citation``). Aucune ``Indexation`` en S3 (S4).
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.extraction_support import (
    POLICY,
    ExtractionRig,
    open_extraction_rig,
)

pytestmark = pytest.mark.integration

COURSE = (
    "# Cours asyncio\n\n## Boucle\n\nLa boucle planifie les coroutines.\n\n"
    "## Tâches\n\nUne tâche enveloppe une coroutine.\n"
).encode()
QA_SHEET = (
    "# Fiche FastAPI\n\n## Question : Qu'est-ce que Depends ?\n\n"
    "Depends injecte une dépendance.\n\n"
    "```python\ndef f(db=Depends(get_db)): ...\n```\n\n"
    "## Question : Qu'est-ce que lifespan ?\n\nIl gère le cycle de vie.\n"
).encode()
TEXT = "Les décorateurs enveloppent une fonction.\n\nUn deuxième paragraphe.\n".encode()


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


async def test_a_received_version_becomes_extracted_with_units_and_chunks(
    rig: ExtractionRig,
) -> None:
    imported = await rig.import_document(COURSE)

    outcome = await rig.run(imported.item_id)

    assert outcome == "succeeded"
    assert (await rig.version(imported.version_id))["status"] == "EXTRACTED"
    item = await rig.item(imported.item_id)
    assert item["status"] == "SUCCEEDED"
    units = await rig.units(imported.version_id)
    chunks = await rig.chunks(imported.version_id)
    assert [u["unit_type"] for u in units] == ["SECTION"]
    assert [list(u["section_path"]) for u in units] == [["Cours asyncio"]]
    assert len(chunks) == 1
    assert chunks[0]["unit_id"] == units[0]["id"]
    assert chunks[0]["chunking_policy_version"] == POLICY.version
    assert units[0]["chunking_policy_version"] == POLICY.version


async def test_nothing_is_written_before_the_item_runs(rig: ExtractionRig) -> None:
    imported = await rig.import_document(COURSE)

    assert await rig.counts() == (0, 0)
    assert (await rig.version(imported.version_id))["status"] == "RECEIVED"
    assert (await rig.item(imported.item_id))["status"] == "PENDING"


async def test_a_qa_sheet_gives_one_unit_per_question_with_its_code(
    rig: ExtractionRig,
) -> None:
    imported = await rig.import_document(QA_SHEET, kind="QA_SHEET")

    await rig.run(imported.item_id)

    units = await rig.units(imported.version_id)
    assert [(u["unit_type"], u["question_text"]) for u in units] == [
        ("QA", "Qu'est-ce que Depends ?"),
        ("QA", "Qu'est-ce que lifespan ?"),
    ]
    assert [u["ordinal"] for u in units] == [0, 1]
    assert "def f(db=Depends(get_db))" in units[0]["text"]
    chunks = await rig.chunks(imported.version_id)
    assert "def f(db=Depends(get_db))" in chunks[0]["contextual_text"]
    assert chunks[0]["contextual_text"].startswith(
        "Python avancé\nFiche FastAPI\nQuestion : Qu'est-ce que Depends ?\n\n"
    )


async def test_every_chunk_is_persisted_with_a_complete_citation(
    rig: ExtractionRig,
) -> None:
    imported = await rig.import_document(QA_SHEET, kind="QA_SHEET")

    await rig.run(imported.item_id)

    for chunk in await rig.chunks(imported.version_id):
        assert chunk["citation"] == {
            "document_id": str(imported.document_id),
            "document_title": "Python avancé",
            "version_id": str(imported.version_id),
            "version_number": 1,
            "section_path": ["Fiche FastAPI"],
            "page_start": None,
            "page_end": None,
            "source_filename": "cours.md",
        }


async def test_chunk_hash_and_token_count_are_stored_as_specified(
    rig: ExtractionRig,
) -> None:
    imported = await rig.import_document(QA_SHEET, kind="QA_SHEET")

    await rig.run(imported.item_id)

    for chunk in await rig.chunks(imported.version_id):
        text = chunk["contextual_text"]
        digest = hashlib.sha256((text + POLICY.version).encode()).hexdigest()
        assert chunk["chunk_hash"] == digest
        assert chunk["token_count"] == len(text.split())
        assert chunk["token_count"] <= POLICY.max_tokens


async def test_a_plain_text_file_is_extracted_without_docling(
    rig: ExtractionRig,
) -> None:
    imported = await rig.import_document(
        TEXT, kind="TECHNICAL", filename="notes.txt", media_type="text/plain"
    )

    outcome = await rig.run(imported.item_id)

    assert outcome == "succeeded"
    (unit,) = await rig.units(imported.version_id)
    assert unit["unit_type"] == "SECTION"
    assert list(unit["section_path"]) == []
    (chunk,) = await rig.chunks(imported.version_id)
    assert chunk["citation"]["source_filename"] == "notes.txt"


async def test_the_real_docling_parser_extracts_a_markdown_qa_sheet(
    rig: ExtractionRig,
) -> None:
    pytest.importorskip("docling")
    imported = await rig.import_document(QA_SHEET, kind="QA_SHEET")

    outcome = await rig.run(imported.item_id, rig.handler_with(None))

    assert outcome == "succeeded"
    units = await rig.units(imported.version_id)
    assert [u["question_text"] for u in units] == [
        "Qu'est-ce que Depends ?",
        "Qu'est-ce que lifespan ?",
    ]
    assert (await rig.version(imported.version_id))["status"] == "EXTRACTED"
