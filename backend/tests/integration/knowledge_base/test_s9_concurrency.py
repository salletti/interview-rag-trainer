"""La ligne du corpus sérialise la migration et les écritures qui la lisent (S9 ;
knowledge-base.md §13 ; ADR 0011 §5) contre PostgreSQL réel.

Aucun ``sleep`` : l'ordre est imposé par des barrières (``asyncio.Event``) et
par le verrou de ligne lui-même (une transaction tient ``FOR SHARE`` ou
``FOR UPDATE`` jusqu'à ce que le test la libère) ; la tenue d'un verrou est
observée par ``NOWAIT`` (erreur ``55P03`` immédiate), et l'ATTENTE d'une
transaction par ``pg_stat_activity`` (``wait_event_type = 'Lock'``), jamais par
une attente chronométrée.

Contrat vérifié :

- ``StartIndexMigration`` et ``ActivateSearchIndex`` tiennent ``FOR UPDATE`` sur
  la ligne du corpus DÈS leur première lecture, avant la preuve de couverture
  et avant toute écriture (aucune lecture partagée, donc aucune extraction ni
  publication, ne passe entre la lecture et la bascule) ;
- une extraction en cours (qui tient ``FOR SHARE``) retarde le provisionnement
  du nouvel index, dont le backfill voit alors sa version : elle est indexée
  dans le nouvel index, une seule fois ;
- une publication en cours (``FOR SHARE``) retarde l'activation, dont la preuve
  de couverture voit alors la version publiée : l'activation est REFUSÉE tant
  que cette version n'est pas ``COMPLETED`` dans le nouvel index (jamais une
  version publiée absente de l'index actif).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from knowledge_base.adapters.gateway.blob.filesystem_blob_store import (
    FileSystemBlobStore,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_extraction_transaction import (  # noqa: E501
    SqlAlchemyExtractionTransaction,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from knowledge_base.domain.entities.knowledge_corpus import CORPUS_ID
from knowledge_base.domain.exceptions.corpus_errors import IncompleteCoverageError
from knowledge_base.use_cases.record_extraction.record_extraction_command import (
    RecordExtraction,
)
from knowledge_base.use_cases.record_extraction.record_extraction_handler import (
    RecordExtractionHandler,
)
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.extraction_support import POLICY
from tests.integration.knowledge_base.extraction_support import (
    test_parser as markdown_parser,
)
from tests.integration.knowledge_base.s9_support import (
    LOCK_NOT_AVAILABLE,
    POLICY_SAME,
    TIMEOUT,
    GatedUnitOfWork,
    HoldFinalization,
    S9Rig,
    as_handler,
    documents_of,
    long_sheet,
    open_s9_rig,
    wait_until_a_backend_waits_for,
)
from tests.unit.knowledge_base.domain.services.support import COUNTER
from tests.unit.knowledge_base.use_cases.s9_support import (
    ACTIVATE,
    PROFILE_B,
    START,
    activate_command,
    contract,
    start_command,
)

pytestmark = pytest.mark.integration

FOR_SHARE_NOWAIT = text(
    "SELECT corpus_id FROM knowledge_base.knowledge_corpus "
    "WHERE corpus_id = :id FOR SHARE NOWAIT"
)
FOR_UPDATE_NOWAIT = text(
    "SELECT corpus_id FROM knowledge_base.knowledge_corpus "
    "WHERE corpus_id = :id FOR UPDATE NOWAIT"
)


@pytest.fixture
async def rig(
    integration_settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[S9Rig]:
    async with open_s9_rig(
        integration_settings, session_factory, tmp_path, monkeypatch
    ) as opened:
        yield opened


def gated_start(rig: S9Rig, **gates: Any) -> Any:
    handler = contract(
        f"{START}.start_index_migration_handler", "StartIndexMigrationHandler"
    )
    ids = iter(range(70_001, 70_100))
    return handler(
        unit_of_work=lambda: GatedUnitOfWork(
            SqlAlchemyUnitOfWork(rig.session_factory), **gates
        ),
        vector_index=rig.vector,
        id_factory=lambda: UUID(int=next(ids)),
        clock=lambda: datetime.now(UTC),
    )


def gated_activate(rig: S9Rig, **gates: Any) -> Any:
    handler = contract(
        f"{ACTIVATE}.activate_search_index_handler", "ActivateSearchIndexHandler"
    )
    return handler(
        unit_of_work=lambda: GatedUnitOfWork(
            SqlAlchemyUnitOfWork(rig.session_factory), **gates
        ),
        clock=lambda: datetime.now(UTC),
    )


async def refused_by_lock(session_factory: SessionFactory, statement: Any) -> bool:
    """Vrai si la ligne du corpus est verrouillée de façon incompatible."""
    try:
        async with session_factory() as other, other.begin():
            await other.execute(statement, {"id": CORPUS_ID})
    except DBAPIError as error:
        return getattr(error.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE
    return False


# --- les verrous du provisionnement et de l'activation -------------------------------


async def test_the_activation_holds_the_corpus_exclusively_from_its_first_read(
    rig: S9Rig, session_factory: SessionFactory
) -> None:
    new = await rig.start(PROFILE_B, POLICY_SAME)
    at_save, release = asyncio.Event(), asyncio.Event()
    activation = asyncio.create_task(
        gated_activate(rig, at_save=at_save, release_save=release).handle(
            activate_command(new)
        )
    )
    try:
        async with asyncio.timeout(TIMEOUT):
            await at_save.wait()  # corpus lu, couverture prouvée, rien d'écrit

        # Aucune extraction ni publication ne peut lire le corpus ici : sans
        # cela, une version publiée entre la preuve et la bascule échapperait
        # au contrôle.
        assert await refused_by_lock(session_factory, FOR_SHARE_NOWAIT)
        assert await refused_by_lock(session_factory, FOR_UPDATE_NOWAIT)
    finally:
        release.set()
    await activation

    assert await rig.active_id() == new
    assert not await refused_by_lock(session_factory, FOR_SHARE_NOWAIT)


async def test_the_provisioning_holds_the_corpus_exclusively_from_its_first_read(
    rig: S9Rig, session_factory: SessionFactory
) -> None:
    await rig.publish(long_sheet("a"))
    at_save, release = asyncio.Event(), asyncio.Event()
    provisioning = asyncio.create_task(
        gated_start(rig, at_save=at_save, release_save=release).handle(
            start_command(PROFILE_B, POLICY_SAME)
        )
    )
    try:
        async with asyncio.timeout(TIMEOUT):
            await at_save.wait()  # corpus lu (backfill pas encore écrit)

        assert await refused_by_lock(session_factory, FOR_SHARE_NOWAIT)
        assert await refused_by_lock(session_factory, FOR_UPDATE_NOWAIT)
        assert await rig.statuses() == {rig.index_id: "ACTIVE"}
    finally:
        release.set()
    new = await provisioning

    assert await rig.statuses() == {rig.index_id: "ACTIVE", new: "BUILDING"}
    assert len(await rig.indexations(new)) == 1


# --- une extraction en cours pendant le provisionnement ------------------------------


async def test_an_extraction_in_flight_is_covered_by_the_backfill_of_the_new_index(
    rig: S9Rig, session_factory: SessionFactory
) -> None:
    published = await rig.publish(long_sheet("a"))
    imported = await rig.import_doc(long_sheet("b"), title="Fiche b")
    use_case = RecordExtractionHandler(
        unit_of_work=lambda: SqlAlchemyUnitOfWork(session_factory),
        blob_reader=FileSystemBlobStore(rig.kb.blob_root),
        parser=markdown_parser(),
        token_counter=COUNTER,
        policy=POLICY,
    )
    prepared = await use_case.prepare(
        RecordExtraction(imported.document_id, imported.version_id)
    )
    assert prepared is not None
    applied, release = asyncio.Event(), asyncio.Event()

    async def extraction_in_flight() -> None:
        async with session_factory() as session, session.begin():
            # Lit les index vivants sous FOR SHARE : seul l'index actif existe.
            assert await use_case.apply(
                SqlAlchemyExtractionTransaction(session), prepared
            )
            applied.set()
            async with asyncio.timeout(TIMEOUT):
                await release.wait()

    extraction = asyncio.create_task(extraction_in_flight())
    provisioning: asyncio.Task[UUID] | None = None
    try:
        async with asyncio.timeout(TIMEOUT):
            await applied.wait()  # l'extraction tient FOR SHARE, version EXTRACTED
        provisioning = asyncio.create_task(
            gated_start(rig).handle(start_command(PROFILE_B, POLICY_SAME))
        )
        # PostgreSQL signale le provisionnement en attente du verrou du corpus
        await wait_until_a_backend_waits_for(session_factory, "FOR UPDATE")
    finally:
        release.set()  # l'extraction valide : le provisionnement peut lire
    await extraction
    assert provisioning is not None
    new = await provisioning

    in_new = await rig.indexations(new)
    assert set(in_new) == {published.version_id, imported.version_id}
    assert {i["status"] for i in in_new.values()} == {"PENDING"}
    items = await rig.items("index_version")
    assert f"index_version:{imported.version_id}:{new}" in items
    assert f"index_version:{imported.version_id}:{rig.index_id}" in items
    await rig.drain(index_id=new)
    assert {i["status"] for i in (await rig.indexations(new)).values()} == {"COMPLETED"}


# --- une publication en cours pendant l'activation -----------------------------------


async def test_a_publication_in_flight_makes_the_activation_see_the_published_version(
    rig: S9Rig, session_factory: SessionFactory
) -> None:
    first = await rig.publish(long_sheet("a"))
    new = await rig.start(PROFILE_B, POLICY_SAME)
    await rig.drain(index_id=new)  # v1 couverte dans le nouvel index
    response = await rig.indexing.extraction.imports.post_version(
        first.document_id, long_sheet("a2")
    )
    assert response.status_code == 202, response.text
    version = UUID(response.json()["version_id"])
    (extract,) = [
        i
        for i in await rig.indexing.extraction.imports.extract_items()
        if i["payload"]["version_id"] == str(version)
    ]
    assert await rig.run_id(extract["id"]) == "succeeded"
    # v2 : EXTRACTED, indexations PENDING dans l'ancien ET le nouvel index.

    reached, release = asyncio.Event(), asyncio.Event()
    held = HoldFinalization(rig.index_handler(), reached, release)
    publication = asyncio.create_task(
        rig.run_index(version, rig.index_id, as_handler(held))
    )
    activation: asyncio.Task[None] | None = None
    try:
        async with asyncio.timeout(TIMEOUT):
            # v2 est COMPLETED et PUBLISHED dans la transaction, encore ouverte
            await reached.wait()
        activation = asyncio.create_task(
            gated_activate(rig).handle(activate_command(new))
        )
        # PostgreSQL signale l'activation en attente du verrou du corpus
        await wait_until_a_backend_waits_for(session_factory, "FOR UPDATE")
    finally:
        release.set()  # la publication valide : l'activation peut lire
    assert await publication == "succeeded"

    assert activation is not None
    with pytest.raises(IncompleteCoverageError):
        await activation

    assert await rig.statuses() == {rig.index_id: "ACTIVE", new: "BUILDING"}
    (document,) = await rig.indexing.extraction.imports.documents()
    assert document["published_version_id"] == version
    assert (await rig.indexations(new))[version]["status"] == "PENDING"

    # la couverture se complète, puis l'activation passe
    await rig.drain(index_id=new)
    await rig.activate(new)
    result = await rig.search()
    assert result.index_id == new
    assert {p.citation.version_id for p in result.passages} == {version}
    assert documents_of(result) == {first.document_id}
