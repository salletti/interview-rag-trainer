"""Slice S2, bout en bout : POST multipart réel -> lignes, blob, work item.

PostgreSQL réel (``compose.test.yaml``), application ``create_app`` complète,
volume de blobs temporaire. Voir ``import_support`` pour le contrat HTTP.

Tables figées par ces tests (schéma ``knowledge_base``) : ``documents``,
``document_versions``, ``idempotency_keys`` (cf. ``test_import_migration``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text

from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.import_support import (
    MAX_UPLOAD_BYTES,
    PREFIX,
    ImportRig,
    blob_path,
    open_import_rig,
    sha256,
)

pytestmark = pytest.mark.integration

V1 = b"# Asyncio\n\nLes coroutines.\n"
V2 = b"# Asyncio\n\nLes coroutines et les taches.\n"
V3 = b"# Asyncio\n\nLes coroutines, taches et groupes.\n"


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


# --- import d'un nouveau document ----------------------------------------------------


async def test_import_answers_202_with_the_identifiers(rig: ImportRig) -> None:
    response = await rig.post_document(V1)

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"document_id", "version_id", "number"}
    assert body["number"] == 1


async def test_import_persists_the_document_and_a_received_version(
    rig: ImportRig,
) -> None:
    body = (await rig.post_document(V1, filename="asyncio.md")).json()

    (document,) = await rig.documents()
    assert str(document["id"]) == body["document_id"]
    assert (document["title"], document["kind"], document["language"]) == (
        "Python avancé",
        "COURSE",
        "fr",
    )
    assert document["status"] == "ACTIVE"
    assert list(document["topics"]) == ["python", "asyncio"]
    (version,) = await rig.versions(body["document_id"])
    assert str(version["id"]) == body["version_id"]
    assert version["number"] == 1
    assert version["status"] == "RECEIVED"
    assert version["content_hash"] == sha256(V1)
    assert version["size_bytes"] == len(V1)
    assert version["filename"] == "asyncio.md"
    assert version["media_type"] == "text/markdown"


async def test_import_stores_the_original_at_the_content_addressed_path(
    rig: ImportRig,
) -> None:
    await rig.post_document(V1)

    assert rig.blob_files() == [blob_path(rig.blob_root, V1)]
    assert blob_path(rig.blob_root, V1).read_bytes() == V1


async def test_import_enqueues_a_pending_extract_version_item(rig: ImportRig) -> None:
    body = (await rig.post_document(V1)).json()

    (item,) = await rig.extract_items()
    assert item["status"] == "PENDING"
    assert item["dedup_key"] == f"extract_version:{body['version_id']}"
    assert item["payload"] == {
        "document_id": body["document_id"],
        "version_id": body["version_id"],
    }


async def test_a_plain_text_file_with_a_charset_parameter_is_accepted(
    rig: ImportRig,
) -> None:
    response = await rig.post_document(
        b"notes brutes\n", filename="notes.txt", media_type="text/plain; charset=utf-8"
    )

    assert response.status_code == 202
    (version,) = await rig.versions(response.json()["document_id"])
    assert version["media_type"] == "text/plain"


async def test_a_file_at_exactly_the_limit_is_accepted(rig: ImportRig) -> None:
    response = await rig.post_document(b"x" * MAX_UPLOAD_BYTES)

    assert response.status_code == 202


# --- même fichier deux fois ----------------------------------------------------------


async def test_the_same_file_twice_returns_the_same_document_with_200(
    rig: ImportRig,
) -> None:
    first = await rig.post_document(V1)

    second = await rig.post_document(V1, title="Autre titre")

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json() == first.json()


async def test_the_same_file_twice_creates_no_second_version_item_or_blob(
    rig: ImportRig,
) -> None:
    first = (await rig.post_document(V1)).json()
    await rig.post_document(V1)

    assert await rig.count("documents") == 1
    assert len(await rig.versions(first["document_id"])) == 1
    assert len(await rig.extract_items()) == 1
    assert rig.blob_files() == [blob_path(rig.blob_root, V1)]


# --- Idempotency-Key -----------------------------------------------------------------


async def test_the_same_key_and_request_replay_the_same_response(
    rig: ImportRig,
) -> None:
    first = await rig.post_document(V1, idempotency_key="key-1")

    second = await rig.post_document(V1, idempotency_key="key-1")

    assert (first.status_code, second.status_code) == (202, 202)
    assert second.json() == first.json()
    assert await rig.count("documents") == 1
    assert await rig.count("idempotency_keys") == 1


async def test_the_same_key_with_another_content_is_a_409_and_changes_nothing(
    rig: ImportRig,
) -> None:
    await rig.post_document(V1, idempotency_key="key-1")

    response = await rig.post_document(V2, idempotency_key="key-1")

    assert response.status_code == 409
    assert response.json()["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert await rig.count("documents") == 1
    assert len(await rig.extract_items()) == 1


async def test_a_replay_after_a_newer_version_still_returns_the_original_response(
    rig: ImportRig,
) -> None:
    first = await rig.post_document(V1, idempotency_key="key-1")
    await rig.post_version(first.json()["document_id"], V2)

    replay = await rig.post_document(V1, idempotency_key="key-1")

    assert replay.status_code == 202
    assert replay.json() == first.json()


# --- limites -------------------------------------------------------------------------


async def test_a_file_over_the_limit_is_a_413_without_blob_or_row(
    rig: ImportRig,
) -> None:
    response = await rig.post_document(b"x" * (MAX_UPLOAD_BYTES + 1))

    assert response.status_code == 413
    assert response.json()["code"] == "PAYLOAD_TOO_LARGE"
    assert rig.blob_files() == []
    assert await rig.count("documents") == 0
    assert await rig.count("document_versions") == 0
    assert await rig.count("work_items") == 0
    assert await rig.count("idempotency_keys") == 0


@pytest.mark.parametrize(
    ("filename", "media_type"),
    [
        # Word binaire historique : seul l'OOXML (.docx) est accepté (S6).
        ("cours.doc", "application/msword"),
        ("cours.md", "application/pdf"),
        ("cours.pdf", "text/markdown"),
        ("cours.html", "text/html"),
    ],
)
async def test_a_refused_type_is_a_415_without_blob_or_row(
    rig: ImportRig, filename: str, media_type: str
) -> None:
    response = await rig.post_document(
        b"%PDF-1.7 ...", filename=filename, media_type=media_type
    )

    assert response.status_code == 415
    assert response.json()["code"] == "UNSUPPORTED_MEDIA_TYPE"
    assert rig.blob_files() == []
    assert await rig.count("documents") == 0
    assert await rig.count("work_items") == 0


async def test_a_missing_title_is_a_422_without_blob_or_row(rig: ImportRig) -> None:
    response = await rig.post_document(V1, omit=("title",))

    assert response.status_code == 422
    assert rig.blob_files() == []
    assert await rig.count("documents") == 0


# --- nouvelle version ----------------------------------------------------------------


async def test_a_new_version_answers_202_with_the_next_number(rig: ImportRig) -> None:
    first = (await rig.post_document(V1)).json()

    response = await rig.post_version(first["document_id"], V2)

    assert response.status_code == 202
    body = response.json()
    assert body["document_id"] == first["document_id"]
    assert body["number"] == 2
    assert body["version_id"] != first["version_id"]
    assert blob_path(rig.blob_root, V2).read_bytes() == V2


async def test_a_new_version_discards_the_previous_one_and_cancels_its_item(
    rig: ImportRig,
) -> None:
    first = (await rig.post_document(V1)).json()
    second = (await rig.post_version(first["document_id"], V2)).json()

    versions = await rig.versions(first["document_id"])
    assert [(v["number"], v["status"]) for v in versions] == [
        (1, "DISCARDED"),
        (2, "RECEIVED"),
    ]
    status_by_key = {i["dedup_key"]: i["status"] for i in await rig.extract_items()}
    assert status_by_key == {
        f"extract_version:{first['version_id']}": "CANCELLED",
        f"extract_version:{second['version_id']}": "PENDING",
    }


async def test_a_published_version_survives_a_new_import_and_keeps_its_item_state(
    rig: ImportRig,
) -> None:
    first = (await rig.post_document(V1)).json()
    async with rig.session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE knowledge_base.document_versions SET status = 'PUBLISHED' "
                "WHERE id = :v"
            ),
            {"v": first["version_id"]},
        )
        await session.execute(
            text(
                "UPDATE knowledge_base.documents SET published_version_id = :v "
                "WHERE id = :d"
            ),
            {"v": first["version_id"], "d": first["document_id"]},
        )

    await rig.post_version(first["document_id"], V2)

    versions = await rig.versions(first["document_id"])
    assert [(v["number"], v["status"]) for v in versions] == [
        (1, "PUBLISHED"),
        (2, "RECEIVED"),
    ]


async def test_versions_of_an_unknown_document_are_a_404(rig: ImportRig) -> None:
    response = await rig.post_version("00000000-0000-4000-8000-000000000999", V1)

    assert response.status_code == 404
    assert response.json()["code"] == "DOCUMENT_NOT_FOUND"
    assert await rig.count("documents") == 0
    assert await rig.count("work_items") == 0


async def test_a_content_already_in_the_same_document_is_a_409_with_its_version(
    rig: ImportRig,
) -> None:
    first = (await rig.post_document(V1)).json()
    await rig.post_version(first["document_id"], V2)

    response = await rig.post_version(first["document_id"], V1)

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "DUPLICATE_CONTENT"
    assert body["document_id"] == first["document_id"]
    assert body["version_id"] == first["version_id"]
    assert len(await rig.versions(first["document_id"])) == 2


async def test_o2_a_content_owned_by_another_document_is_a_409_with_its_id(
    rig: ImportRig,
) -> None:
    owner = (await rig.post_document(V1, title="Document A")).json()
    target = (await rig.post_document(V2, title="Document B")).json()

    response = await rig.post_version(target["document_id"], V1)

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "DUPLICATE_CONTENT"
    assert body["document_id"] == owner["document_id"]
    assert body["version_id"] == owner["version_id"]
    assert len(await rig.versions(target["document_id"])) == 1
    assert len(await rig.versions(owner["document_id"])) == 1
    assert await rig.count("documents") == 2


async def test_a_version_of_a_withdrawn_document_is_a_409_and_changes_nothing(
    rig: ImportRig,
) -> None:
    first = (await rig.post_document(V1)).json()
    async with rig.session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE knowledge_base.documents SET status = 'WITHDRAWN' WHERE id = :d"
            ),
            {"d": first["document_id"]},
        )

    response = await rig.post_version(first["document_id"], V2)

    assert response.status_code == 409
    assert response.json()["code"] == "DOCUMENT_WITHDRAWN"
    assert len(await rig.versions(first["document_id"])) == 1
    assert [i["status"] for i in await rig.extract_items()] == ["PENDING"]


async def test_reimporting_the_content_of_a_withdrawn_document_is_a_409(
    rig: ImportRig,
) -> None:
    # Revue S2 M-7 ; le retrait est fait en base (WithdrawDocument arrive en S7).
    first = (await rig.post_document(V1)).json()
    async with rig.session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE knowledge_base.documents SET status = 'WITHDRAWN' WHERE id = :d"
            ),
            {"d": first["document_id"]},
        )

    response = await rig.post_document(V1, title="Autre titre", idempotency_key="k")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "DOCUMENT_WITHDRAWN"
    assert body["document_id"] == first["document_id"]
    assert await rig.count("documents") == 1
    assert await rig.count("idempotency_keys") == 0
    assert [i["status"] for i in await rig.extract_items()] == ["PENDING"]


async def test_a_path_in_the_filename_is_dropped_before_storage(
    rig: ImportRig,
) -> None:
    body = (await rig.post_document(V1, filename="..\\..\\notes/asyncio.md")).json()

    (version,) = await rig.versions(body["document_id"])
    assert version["filename"] == "asyncio.md"


async def test_a_filename_over_255_characters_is_a_422_without_blob_or_row(
    rig: ImportRig,
) -> None:
    response = await rig.post_document(V1, filename="x" * 253 + ".md")

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_FILENAME"
    assert rig.blob_files() == []
    assert await rig.count("documents") == 0


async def test_a_refused_type_on_a_version_is_a_415_and_nothing_changes(
    rig: ImportRig,
) -> None:
    first = (await rig.post_document(V1)).json()

    response = await rig.post_version(
        # PDF accepté depuis S6 : le Word binaire historique reste refusé.
        first["document_id"],
        V2,
        filename="v2.doc",
        media_type="application/msword",
    )

    assert response.status_code == 415
    assert len(await rig.versions(first["document_id"])) == 1
    assert rig.blob_files() == [blob_path(rig.blob_root, V1)]


async def test_the_same_key_on_a_version_replays_without_a_third_version(
    rig: ImportRig,
) -> None:
    first = (await rig.post_document(V1)).json()
    one = await rig.post_version(first["document_id"], V2, idempotency_key="key-v")
    two = await rig.post_version(first["document_id"], V2, idempotency_key="key-v")

    assert (one.status_code, two.status_code) == (202, 202)
    assert two.json() == one.json()
    assert len(await rig.versions(first["document_id"])) == 2


async def test_routes_live_under_the_versioned_prefix(rig: ImportRig) -> None:
    assert (await rig.client.post("/documents")).status_code == 404
    assert (await rig.client.post(f"{PREFIX}/documents")).status_code == 422
