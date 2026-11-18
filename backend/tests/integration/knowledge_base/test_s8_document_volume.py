"""Borne de volume de texte câblée de bout en bout (S8, O16), sur PostgreSQL
réel : ``KB_MAX_DOCUMENT_TOKENS`` (``settings.max_document_tokens``) alimente
``build_extract_version_handler`` ; un document au-delà échoue en UNE tentative,
terminal, non rejouable, sans chunk écrit. Le détail de la mesure est dans
``tests/unit/knowledge_base/test_document_volume_limit``.
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


async def test_a_document_over_the_limit_fails_once_and_cannot_be_replayed(
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
    assert version["failure_retryable"] is False
    assert await rig.counts() == (0, 0)


async def test_the_same_document_under_the_limit_is_extracted(
    rig: ExtractionRig,
) -> None:
    imported = await rig.import_document(BIG)

    outcome = await rig.run(imported.item_id, handler_with(rig, limit=100_000))

    assert outcome == "succeeded"
    assert (await rig.version(imported.version_id))["status"] == "EXTRACTED"
