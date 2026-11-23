"""``CleanStaleUploads`` : suppression des temporaires de dépôt abandonnés (S8 ;
roadmap S8 « temporaires de ``tmp/`` abandonnés » ; ADR 0012).

Un import écrit son flux dans ``tmp/`` puis le renomme : un crash ou une
coupure en laisse un fichier partiel que personne ne réclamera. Un temporaire
RÉCENT peut être un import en cours : il est protégé par le délai de grâce (le
même que celui des blobs orphelins, ``KB_BLOB_ORPHAN_GRACE_SECONDS``).

API figée (``knowledge_base.use_cases.clean_stale_uploads``) :

- ``clean_stale_uploads_command.CleanStaleUploads(grace: timedelta)``.
- ``clean_stale_uploads_handler.CleanStaleUploadsHandler(*, staging, clock)`` ;
  ``await handle(command) -> int`` : nombre de temporaires supprimés. Un
  temporaire est supprimé si et seulement si ``clock() - modified_at >
  grace`` (strictement, comme ``CleanBlobOrphans``). ``staging`` est un
  ``StagedUploadStore`` (``list_staged()``, ``delete_staged(name)``).
- Répétable sans effet. Le handler n'importe aucun module du système de
  fichiers (test d'architecture).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from knowledge_base.use_cases.clean_stale_uploads.clean_stale_uploads_command import (
    CleanStaleUploads,
)
from knowledge_base.use_cases.clean_stale_uploads.clean_stale_uploads_handler import (
    CleanStaleUploadsHandler,
)

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
GRACE = timedelta(hours=24)


@dataclass(frozen=True)
class Staged:
    name: str
    modified_at: datetime


class FakeStaging:
    """Le volume : ``tmp/`` en mémoire ; ``deleted`` journalise les appels."""

    def __init__(self, **ages: timedelta) -> None:
        self.files = {name: NOW - age for name, age in ages.items()}
        self.deleted: list[str] = []

    async def list_staged(self) -> Sequence[Staged]:
        return [Staged(name, at) for name, at in self.files.items()]

    async def delete_staged(self, name: str) -> bool:
        self.deleted.append(name)
        return self.files.pop(name, None) is not None


def cleaner(staging: FakeStaging) -> CleanStaleUploadsHandler:
    return CleanStaleUploadsHandler(staging=staging, clock=lambda: NOW)


async def test_an_abandoned_upload_older_than_the_grace_period_is_deleted() -> None:
    staging = FakeStaging(old=GRACE + timedelta(seconds=1))

    deleted = await cleaner(staging).handle(CleanStaleUploads(grace=GRACE))

    assert deleted == 1
    assert staging.files == {}


async def test_an_upload_within_the_grace_period_is_kept_it_may_be_running() -> None:
    staging = FakeStaging(young=GRACE - timedelta(seconds=1))

    deleted = await cleaner(staging).handle(CleanStaleUploads(grace=GRACE))

    assert deleted == 0
    assert set(staging.files) == {"young"}
    assert staging.deleted == []


async def test_an_upload_exactly_as_old_as_the_grace_period_is_kept() -> None:
    staging = FakeStaging(edge=GRACE)

    await cleaner(staging).handle(CleanStaleUploads(grace=GRACE))

    assert set(staging.files) == {"edge"}


async def test_only_the_old_uploads_are_deleted_and_counted() -> None:
    staging = FakeStaging(
        a=GRACE + timedelta(hours=5),
        b=GRACE + timedelta(minutes=1),
        c=timedelta(minutes=5),
    )

    deleted = await cleaner(staging).handle(CleanStaleUploads(grace=GRACE))

    assert deleted == 2
    assert set(staging.files) == {"c"}


async def test_a_file_that_vanished_meanwhile_is_not_counted() -> None:
    class Racing(FakeStaging):
        async def delete_staged(self, name: str) -> bool:
            self.files.pop(name, None)  # un import concurrent l'a renommé
            return False

    racing = Racing(old=GRACE + timedelta(hours=1))

    assert await cleaner(racing).handle(CleanStaleUploads(grace=GRACE)) == 0


async def test_a_second_cleaning_changes_nothing() -> None:
    staging = FakeStaging(old=GRACE + timedelta(hours=1), young=timedelta(minutes=1))
    await cleaner(staging).handle(CleanStaleUploads(grace=GRACE))

    second = await cleaner(staging).handle(CleanStaleUploads(grace=GRACE))

    assert second == 0
    assert set(staging.files) == {"young"}
