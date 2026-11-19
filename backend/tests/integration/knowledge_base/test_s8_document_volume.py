"""Borne de volume de texte câblée de bout en bout (S8, O16), sur PostgreSQL
réel : ``KB_MAX_DOCUMENT_TOKENS`` (``settings.max_document_tokens``) alimente
``build_extract_version_handler`` ; un document au-delà échoue en UNE tentative,
terminal (jamais réessayé seul), sans chunk écrit. Le détail de la mesure est
dans ``tests/unit/knowledge_base/test_document_volume_limit``.

Revue S8 (I1) : l'échec est REJOUABLE. Le même contenu ne peut pas être
réimporté (O2 : contenu connu -> version existante) ; la voie de reprise est
de relever ``KB_MAX_DOCUMENT_TOKENS`` puis de rejouer l'item
(``POST /failures/{id}/retry``), jusqu'à la publication.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from knowledge_base.adapters.controller.tasks.extract_version_handler import (
    build_extract_version_handler,
)
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.extraction_support import (
    POLICY,
    ExtractionRig,
    open_extraction_rig,
)
from tests.integration.knowledge_base.extraction_support import (
    test_parser as make_test_parser,
)
from tests.integration.knowledge_base.indexing_support import QA_SHEET
from tests.integration.knowledge_base.s8_support import PREFIX, open_s8_rig
from tests.integration.knowledge_base.support import kb_settings
from tests.unit.knowledge_base.domain.services.support import COUNTER, words
from tests.unit.knowledge_base.s8_support import TEXT_VOLUME_CODE

pytestmark = pytest.mark.integration
BIG = f"# Cours\n\n{words(80)}\n".encode()


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


def handler_with(rig: ExtractionRig, limit: int):  # type: ignore[no-untyped-def]
    return build_extract_version_handler(
        rig.session_factory,
        kb_settings(work_backoff_base_seconds=0.01, max_document_tokens=limit),
        parser=make_test_parser(),
        token_counter=COUNTER,
        policy=POLICY,
    )


async def test_a_document_over_the_limit_fails_once_and_is_replayable(
    rig: ExtractionRig,
) -> None:
    imported = await rig.import_document(BIG)

    outcome = await rig.run(imported.item_id, handler_with(rig, limit=20))

    assert outcome == "failed"
    item = await rig.item(imported.item_id)
    assert (item["status"], item["attempts"]) == ("FAILED", 1)
    assert item["last_error_code"] == TEXT_VOLUME_CODE
    version = await rig.version(imported.version_id)
    assert (version["status"], version["failure_code"]) == ("FAILED", TEXT_VOLUME_CODE)
    assert version["failure_retryable"] is True
    assert await rig.counts() == (0, 0)
    assert await rig.run(imported.item_id) == "skipped"  # jamais rejoué seul


async def test_the_same_document_under_the_limit_is_extracted(
    rig: ExtractionRig,
) -> None:
    imported = await rig.import_document(BIG)

    outcome = await rig.run(imported.item_id, handler_with(rig, limit=100_000))

    assert outcome == "succeeded"
    assert (await rig.version(imported.version_id))["status"] == "EXTRACTED"


async def test_raising_the_limit_then_replaying_publishes_the_document(
    integration_settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with open_s8_rig(
        integration_settings, session_factory, tmp_path, monkeypatch
    ) as s8:
        extraction = s8.s7.indexing.extraction
        imported = await extraction.import_document(QA_SHEET, kind="QA_SHEET")
        narrow = extraction.settings.model_copy(update={"max_document_tokens": 1})
        refused = await extraction.run(
            imported.item_id,
            build_extract_version_handler(
                s8.session_factory,
                narrow,
                parser=make_test_parser(),
                token_counter=COUNTER,
                policy=POLICY,
            ),
        )
        assert refused == "failed"
        failed = await s8.version(imported.version_id)
        assert (failed["failure_code"], failed["failure_retryable"]) == (
            TEXT_VOLUME_CODE,
            True,
        )
        listed = (await s8.http.get(f"{PREFIX}/failures")).json()["items"]
        assert [(i["item_id"], i["replayable"]) for i in listed] == [
            (str(imported.item_id), True)
        ]

        # Borne relevée : les handlers du rig ont la borne par défaut.
        assert extraction.settings.max_document_tokens > 1
        retried = await s8.http.post(f"{PREFIX}/failures/{imported.item_id}/retry")
        assert retried.status_code == 202, retried.text
        await s8.drain()

        assert (await s8.version(imported.version_id))["status"] == "PUBLISHED"
        assert await s8.found() == {imported.document_id}
