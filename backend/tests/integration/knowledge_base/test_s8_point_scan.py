"""Parcours des points d'un index Qdrant, pour la réconciliation (S8 ;
knowledge-base.md §9 : « points en trop pour une version (``scroll``) »), sur
Qdrant réel.

Contrat (``QdrantVectorIndex``, port ``PointInventory`` de
``knowledge_base.domain.providers.point_inventory``) :

- ``scan_points(index_id, *, batch_size=256) -> AsyncIterator[PointRecord]`` :
  tous les points de la collection, sans vecteur, paginés par ``batch_size`` ;
  chaque point exactement une fois.
- ``PointRecord`` (``knowledge_base.domain.value_objects.point_record``) :
  ``point_id``, ``document_id``, ``version_id`` (UUID), ``chunk_hash`` (texte),
  ``visible`` (booléen).
- ``VectorIndex`` n'est PAS élargi : les doublures de ``VectorIndex`` des tests
  existants (``GatedVectorIndex``…) restent valides ; seul l'adapter réel
  porte le parcours.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.s8_support import S8Rig, open_s8_rig

pytestmark = pytest.mark.integration


@pytest.fixture
async def rig(
    integration_settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[S8Rig]:
    async with open_s8_rig(
        integration_settings, session_factory, tmp_path, monkeypatch
    ) as opened:
        yield opened


async def test_every_point_is_returned_once_across_several_batches(rig: S8Rig) -> None:
    document, version = uuid.uuid4(), uuid.uuid4()
    points = [rig.fake_point(document, version) for _ in range(5)]
    await rig.write_points(*points)

    scanned = [p async for p in rig.vector.scan_points(rig.index_id, batch_size=2)]

    assert sorted(str(p.point_id) for p in scanned) == sorted(
        str(p.point_id) for p in points
    )


async def test_a_point_carries_its_owner_its_hash_and_its_visibility(
    rig: S8Rig,
) -> None:
    shown_doc, shown_version = uuid.uuid4(), uuid.uuid4()
    hidden_doc, hidden_version = uuid.uuid4(), uuid.uuid4()
    shown = rig.fake_point(shown_doc, shown_version)
    hidden = rig.fake_point(hidden_doc, hidden_version)
    await rig.write_points(shown, hidden)
    await rig.vector.set_visibility(
        rig.index_id, document_id=shown_doc, version_id=shown_version, visible=True
    )

    scanned = {p.point_id: p async for p in rig.vector.scan_points(rig.index_id)}

    assert {
        pid: (p.document_id, p.version_id, p.chunk_hash, p.visible)
        for pid, p in scanned.items()
    } == {
        shown.point_id: (shown_doc, shown_version, shown.chunk_hash, True),
        hidden.point_id: (hidden_doc, hidden_version, hidden.chunk_hash, False),
    }


async def test_an_empty_collection_yields_nothing(rig: S8Rig) -> None:
    assert [p async for p in rig.vector.scan_points(rig.index_id)] == []
