"""Original absent du volume (PostgreSQL et volume de blobs réels) : échec
TERMINAL ``BLOB_MISSING`` dès la première tentative, version ``FAILED`` avec
``retryable`` vrai (une restauration du blob rend ``RetryIngestion`` utile),
aucune unité ni chunk."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.extraction_support import (
    ExtractionRig,
    open_extraction_rig,
)
from tests.integration.knowledge_base.import_support import blob_path

pytestmark = pytest.mark.integration

COURSE = b"# Cours asyncio\n\nLe blob de ce cours disparait du volume.\n"


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


async def test_a_missing_original_fails_terminally_on_the_first_attempt(
    rig: ExtractionRig,
) -> None:
    imported = await rig.import_document(COURSE)
    blob_path(rig.imports.blob_root, COURSE).unlink()

    outcome = await rig.run(imported.item_id)

    assert outcome == "failed"
    item = await rig.item(imported.item_id)
    assert (item["status"], item["attempts"]) == ("FAILED", 1)
    assert item["last_error_code"] == "BLOB_MISSING"
    version = await rig.version(imported.version_id)
    assert version["status"] == "FAILED"
    assert version["failure_code"] == "BLOB_MISSING"
    assert version["failure_retryable"] is True
    assert await rig.counts() == (0, 0)
