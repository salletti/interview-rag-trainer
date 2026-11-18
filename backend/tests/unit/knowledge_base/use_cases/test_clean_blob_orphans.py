"""Nettoyage des blobs orphelins (S7 ; ADR 0012 point 6 et « Conséquences » :
« un fichier peut exister sans ligne PostgreSQL ; l'inverse est impossible »).

API figée (``knowledge_base.use_cases.clean_blob_orphans``) :

- ``clean_blob_orphans_command.CleanBlobOrphans(grace: timedelta)``.
- ``clean_blob_orphans_handler.CleanBlobOrphansHandler(*, inventory, references,
  eraser, clock)`` ; ``await handle(command)``. Pour chaque blob de
  ``inventory.list_blobs()`` (``content_hash``, ``modified_at``) : supprimé par
  ``eraser.delete`` si et seulement si (a) il a plus de ``grace`` (``clock() -
  modified_at``), ET (b) ``references.holders(content_hash)`` est vide : aucune
  version, de quelque document et dans quelque état que ce soit, ne le porte. Un
  blob jeune est protégé (dépôt d'un import dont la transaction n'a pas encore
  commité, ADR 0012 « Risques ») ; un blob référencé l'est toujours, même
  ancien, y compris par la version d'un document retiré dont la purge n'a pas
  eu lieu. ``holders`` ne compte plus un document dont la purge est
  enregistrée (``purged_at``) : un import refusé du contenu d'un document purgé
  (409, le tombstone garde l'empreinte) redépose le fichier AVANT la
  transaction ; sans cette règle ce blob resterait pour toujours. Répétable
  sans effet. Les temporaires de ``tmp/`` ne sont pas traités
  ici (non couvert par S7).

Le déclenchement périodique est celui de la réconciliation (S8) : S7 livre le
cas d'usage, pas son ordonnancement.
"""

from __future__ import annotations

from datetime import timedelta

from knowledge_base.use_cases.clean_blob_orphans.clean_blob_orphans_command import (
    CleanBlobOrphans,
)
from knowledge_base.use_cases.clean_blob_orphans.clean_blob_orphans_handler import (
    CleanBlobOrphansHandler,
)
from tests.unit.knowledge_base.use_cases.fakes import sha256
from tests.unit.knowledge_base.use_cases.indexing_fakes import qa_sheet
from tests.unit.knowledge_base.use_cases.s7_support import (
    GRACE_BASE,
    S7Rig,
    make_s7_rig,
)

GRACE = timedelta(hours=24)


def cleaner(rig: S7Rig) -> CleanBlobOrphansHandler:
    return CleanBlobOrphansHandler(
        inventory=rig.blobs,
        references=rig.references,
        eraser=rig.blobs,
        clock=lambda: GRACE_BASE,
    )


async def clean(rig: S7Rig) -> None:
    await cleaner(rig).handle(CleanBlobOrphans(grace=GRACE))


def orphan(rig: S7Rig, name: str, *, age: timedelta) -> str:
    digest = sha256(name.encode())
    rig.blobs.drop_file(digest, name.encode(), at=GRACE_BASE - age)
    return digest


async def test_an_orphan_older_than_the_grace_period_is_deleted() -> None:
    rig = make_s7_rig()
    old = orphan(rig, "vieux", age=GRACE + timedelta(seconds=1))

    await clean(rig)

    assert old not in rig.blobs.present


async def test_an_orphan_within_the_grace_period_is_kept() -> None:
    rig = make_s7_rig()
    young = orphan(rig, "jeune", age=GRACE - timedelta(seconds=1))

    await clean(rig)

    assert young in rig.blobs.present


async def test_a_referenced_blob_is_kept_however_old_it_is() -> None:
    rig = make_s7_rig()
    command = await rig.base.import_document(qa_sheet())
    digest = rig.base.document(command.document_id).versions[0].source.content_hash
    rig.blobs.modified[digest.value] = GRACE_BASE - timedelta(days=365)

    await clean(rig)

    assert digest.value in rig.blobs.present


async def test_the_blob_of_a_withdrawn_document_not_yet_purged_is_kept() -> None:
    rig = make_s7_rig()
    command = await rig.base.import_document(qa_sheet())
    digest = rig.base.document(command.document_id).versions[0].source.content_hash
    rig.mark_withdrawn(command.document_id)
    rig.blobs.modified[digest.value] = GRACE_BASE - timedelta(days=30)

    await clean(rig)

    assert digest.value in rig.blobs.present


async def test_the_blob_of_a_purged_document_that_reappeared_is_an_orphan() -> None:
    rig = make_s7_rig()
    command = await rig.base.import_document(qa_sheet())
    digest = rig.base.document(command.document_id).versions[0].source.content_hash
    rig.mark_withdrawn(command.document_id)
    await rig.run_purge(command.document_id)
    assert digest.value not in rig.blobs.present
    # Un import refusé (409) redépose le fichier avant sa transaction.
    rig.blobs.drop_file(digest.value, at=GRACE_BASE - GRACE - timedelta(seconds=1))

    await clean(rig)

    assert digest.value not in rig.blobs.present


async def test_only_the_orphans_are_deleted_among_all_the_blobs() -> None:
    rig = make_s7_rig()
    command = await rig.base.import_document(qa_sheet())
    digest = rig.base.document(command.document_id).versions[0].source.content_hash
    rig.blobs.modified[digest.value] = GRACE_BASE - timedelta(days=2)
    old = orphan(rig, "vieux", age=timedelta(days=2))
    young = orphan(rig, "jeune", age=timedelta(minutes=5))

    await clean(rig)

    assert rig.blobs.present == {digest.value, young}
    assert old in rig.blobs.delete_calls
    assert sorted(rig.blobs.delete_calls) == [old]


async def test_a_second_run_changes_nothing() -> None:
    rig = make_s7_rig()
    orphan(rig, "vieux", age=timedelta(days=2))
    young = orphan(rig, "jeune", age=timedelta(minutes=5))
    await clean(rig)
    calls = list(rig.blobs.delete_calls)

    await clean(rig)

    assert rig.blobs.present == {young}
    assert rig.blobs.delete_calls == calls
