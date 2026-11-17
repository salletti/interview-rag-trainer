"""Atomicité et concurrence de l'import (S2, ADR 0012 §3, §6).

- Écriture du blob AVANT la transaction : un échec de la transaction laisse un
  blob orphelin (repérable sous ``KB_BLOB_ROOT``, purgé par la réconciliation
  S7/S8) et AUCUNE ligne : ni document, ni version, ni work item, ni clé
  d'idempotence, ni annulation de l'item de la version précédente.
- Concurrence (choix) : deux imports concurrents de versions d'un même
  document -> le verrou optimiste (``documents.version``) en laisse passer un ;
  l'autre reçoit 409 ``CONCURRENT_MODIFICATION``, sans réessai automatique (le
  client rejoue). La détection déterministe est testée sur le dépôt
  (``test_import_persistence``) ; ici, l'invariant tient quel que soit
  l'entrelacement.
- Course sur un même nouveau fichier (arbitrage S2) : les deux imports lisent
  « contenu inconnu » (barrière explicite après la lecture), le perdant bute
  sur ``UNIQUE(content_hash)``, rejoue sa transaction et rend le document du
  gagnant en 200 ; aucune ``IntegrityError`` ne remonte.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import text

from knowledge_base.adapters.gateway.persistence.sqlalchemy_knowledge_document_repository import (  # noqa: E501
    SqlAlchemyKnowledgeDocumentRepository,
)
from knowledge_base.domain.entities.knowledge_document import KnowledgeDocument
from knowledge_base.domain.value_objects.content_hash import ContentHash
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.import_support import (
    ImportRig,
    blob_path,
    open_import_rig,
)

pytestmark = pytest.mark.integration

V1 = b"# Version 1\n"
V2 = b"# Version 2\n"


@pytest.fixture
async def rig(
    integration_settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[ImportRig]:
    async with open_import_rig(
        integration_settings, session_factory, tmp_path, monkeypatch
    ) as opened:
        yield opened


@asynccontextmanager
async def enqueue_of_extract_version_fails(
    factory: SessionFactory,
) -> AsyncIterator[None]:
    """Fait échouer, côté PostgreSQL, l'INSERT de l'item ``extract_version`` :
    l'import échoue après avoir écrit le document, donc en pleine transaction."""
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "CREATE OR REPLACE FUNCTION public.kb_test_refuse_extract() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                "RAISE EXCEPTION 'kb_test: enqueue refuse'; END $$"
            )
        )
        await session.execute(
            text(
                "CREATE TRIGGER kb_test_refuse_extract BEFORE INSERT "
                "ON knowledge_base.work_items FOR EACH ROW "
                "WHEN (NEW.kind = 'extract_version') "
                "EXECUTE FUNCTION public.kb_test_refuse_extract()"
            )
        )
    try:
        yield
    finally:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "DROP TRIGGER IF EXISTS kb_test_refuse_extract "
                    "ON knowledge_base.work_items"
                )
            )
            await session.execute(
                text("DROP FUNCTION IF EXISTS public.kb_test_refuse_extract()")
            )


async def test_a_failed_transaction_leaves_an_orphan_blob_and_no_row(
    rig: ImportRig,
) -> None:
    async with enqueue_of_extract_version_fails(rig.session_factory):
        response = await rig.post_document(V1, idempotency_key="key-1")

    assert response.status_code == 500
    assert rig.blob_files() == [blob_path(rig.blob_root, V1)]
    assert await rig.count("documents") == 0
    assert await rig.count("document_versions") == 0
    assert await rig.count("work_items") == 0
    assert await rig.count("idempotency_keys") == 0


async def test_the_same_request_succeeds_after_a_failed_transaction(
    rig: ImportRig,
) -> None:
    async with enqueue_of_extract_version_fails(rig.session_factory):
        await rig.post_document(V1, idempotency_key="key-1")

    retry = await rig.post_document(V1, idempotency_key="key-1")

    assert retry.status_code == 202
    assert await rig.count("documents") == 1
    assert rig.blob_files() == [blob_path(rig.blob_root, V1)]


async def test_a_failed_version_import_discards_and_cancels_nothing(
    rig: ImportRig,
) -> None:
    first = (await rig.post_document(V1)).json()

    async with enqueue_of_extract_version_fails(rig.session_factory):
        response = await rig.post_version(first["document_id"], V2)

    assert response.status_code == 500
    versions = await rig.versions(first["document_id"])
    assert [(v["number"], v["status"]) for v in versions] == [(1, "RECEIVED")]
    assert [i["status"] for i in await rig.extract_items()] == ["PENDING"]
    assert blob_path(rig.blob_root, V2).exists()


async def test_concurrent_version_imports_keep_a_single_live_version(
    rig: ImportRig,
) -> None:
    first = (await rig.post_document(V1)).json()
    contents = [f"# Concurrente {n}\n".encode() for n in range(5)]

    responses = await asyncio.gather(
        *(rig.post_version(first["document_id"], c) for c in contents)
    )

    codes = [r.status_code for r in responses]
    assert set(codes) <= {202, 409}, [r.text for r in responses]
    assert 202 in codes
    for response in responses:
        if response.status_code == 409:
            assert response.json()["code"] == "CONCURRENT_MODIFICATION"
    accepted = [r.json() for r in responses if r.status_code == 202]
    versions = await rig.versions(first["document_id"])
    assert [v["number"] for v in versions] == list(range(1, len(accepted) + 2))
    assert sorted(a["number"] for a in accepted) == list(range(2, len(accepted) + 2))
    live = [v for v in versions if v["status"] == "RECEIVED"]
    assert [v["number"] for v in live] == [len(accepted) + 1]
    pending = [i for i in await rig.extract_items() if i["status"] == "PENDING"]
    assert [i["dedup_key"] for i in pending] == [f"extract_version:{live[0]['id']}"]


def both_lookups_meet_at_a_barrier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Les deux premières recherches par empreinte s'attendent après leur
    lecture : chaque import a vu « contenu inconnu » avant que l'un d'eux
    n'écrive. Les recherches suivantes (rejeu du perdant) passent."""
    original = SqlAlchemyKnowledgeDocumentRepository.find_by_content_hash
    barrier = asyncio.Barrier(2)
    calls = 0

    async def meeting(
        self: SqlAlchemyKnowledgeDocumentRepository, content_hash: ContentHash
    ) -> KnowledgeDocument | None:
        nonlocal calls
        calls += 1
        found = await original(self, content_hash)
        if calls <= 2:
            async with asyncio.timeout(10):
                await barrier.wait()
        return found

    monkeypatch.setattr(
        SqlAlchemyKnowledgeDocumentRepository, "find_by_content_hash", meeting
    )


async def test_two_concurrent_imports_of_the_same_new_file_yield_one_document(
    rig: ImportRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    both_lookups_meet_at_a_barrier(monkeypatch)

    first, second = await asyncio.gather(
        rig.post_document(V1, title="Premier"),
        rig.post_document(V1, title="Second"),
    )

    assert sorted([first.status_code, second.status_code]) == [200, 202], [
        first.text,
        second.text,
    ]
    assert first.json() == second.json()
    (document,) = await rig.documents()
    assert str(document["id"]) == first.json()["document_id"]
    assert len(await rig.versions(document["id"])) == 1
    assert len(await rig.extract_items()) == 1
    assert rig.blob_files() == [blob_path(rig.blob_root, V1)]
