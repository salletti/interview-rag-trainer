"""Échecs de ``extract_version`` (PostgreSQL réel) : un document vide ou
illisible est un échec TERMINAL sans retry qui marque la version ``FAILED`` avec
sa ``FailureReason`` (hook ``on_terminal_failure``) ; une panne passagère est
retentée et ne laisse aucun état partiel.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from knowledge_base.domain.value_objects.structured_document import StructuredDocument
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.extraction_support import (
    ExtractionRig,
    open_extraction_rig,
    test_parser,
)

pytestmark = pytest.mark.integration

COURSE = b"# Cours asyncio\n\nLes coroutines ne bloquent pas la boucle.\n"


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


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"\n  \n\t\n", "EMPTY_DOCUMENT"),
        (b"---\nkind: qa_sheet\n---\n", "EMPTY_DOCUMENT"),
        (b"# Caf\xe9\n\ntexte\n", "INVALID_DOCUMENT"),
    ],
    ids=["blank", "front-matter-only", "not-utf8"],
)
async def test_an_unusable_markdown_fails_terminally_without_retry(
    rig: ExtractionRig, content: bytes, code: str
) -> None:
    imported = await rig.import_document(content)

    outcome = await rig.run(imported.item_id)

    assert outcome == "failed"
    item = await rig.item(imported.item_id)
    assert item["status"] == "FAILED"
    assert item["attempts"] == 1
    assert item["last_error_code"] == code
    version = await rig.version(imported.version_id)
    assert version["status"] == "FAILED"
    assert version["failure_code"] == code
    assert version["failure_message"]
    assert version["failure_retryable"] is False
    assert await rig.counts() == (0, 0)


async def test_a_failed_item_is_not_executed_again(rig: ExtractionRig) -> None:
    imported = await rig.import_document(b"\n \n")
    await rig.run(imported.item_id)

    assert await rig.run(imported.item_id) == "skipped"
    assert (await rig.item(imported.item_id))["attempts"] == 1


class FlakyParser:
    """Échoue (panne passagère) aux ``failures`` premiers appels."""

    def __init__(self, failures: int) -> None:
        self._inner = test_parser()
        self.failures = failures
        self.calls = 0

    def parse(
        self, content: bytes, media_type: str, filename: str
    ) -> StructuredDocument:
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError("volume momentanément indisponible")
        return self._inner.parse(content, media_type, filename)


async def test_a_transient_failure_is_retried_and_leaves_no_partial_state(
    rig: ExtractionRig,
) -> None:
    imported = await rig.import_document(COURSE)
    flaky = FlakyParser(failures=1)
    handler = rig.handler_with(flaky)

    first = await rig.run(imported.item_id, handler)

    assert first == "retry_scheduled"
    assert await rig.counts() == (0, 0)
    assert (await rig.version(imported.version_id))["status"] == "RECEIVED"
    item = await rig.item(imported.item_id)
    assert (item["status"], item["attempts"]) == ("PENDING", 1)

    await asyncio.sleep(0.05)  # backoff de 10 ms (réglage de test)
    second = await rig.run(imported.item_id, handler)

    assert second == "succeeded"
    assert (await rig.version(imported.version_id))["status"] == "EXTRACTED"
    assert (await rig.item(imported.item_id))["attempts"] == 2
    assert len(await rig.chunks(imported.version_id)) == 1
