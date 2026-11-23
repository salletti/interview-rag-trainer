"""Borne de volume de texte commune à tous les formats (S8, O16 ;
knowledge-base.md §16 ; ADR 0012, suivi S6).

La limite d'octets (20 Mio) et celle de pages (300, PDF seulement) ne bornent
pas le TEXTE extrait : un DOCX textuel de quelques Mio de XML dépasse déjà de
loin 300 pages, et le lot d'unités et de chunks est écrit dans UNE transaction
(§3.4). La borne s'applique donc APRÈS l'extraction, sur le même compteur que
le chunking, quel que soit le format.

API figée :

- ``RecordExtractionHandler(..., max_document_tokens: int | None = None)`` :
  ``None`` = pas de borne (comportement actuel, tous les tests existants).
  Dans ``prepare``, APRÈS la segmentation, la somme des ``token_count`` des
  UNITÉS (le texte du document, sans les en-têtes contextuels répétés dans
  chaque chunk, qui gonfleraient la mesure) est comparée à la borne : au-delà
  (strictement), l'extraction est refusée AVANT toute écriture. Exactement
  égale : acceptée.
- Le refus devient, côté work queue, ``TerminalWorkError`` de code
  ``DOCUMENT_TOO_LARGE`` (décision ouverte, voir ``s8_support`` : alternative
  ``DOCUMENT_TOO_COMPLEX``) : aucun retry, version ``FAILED`` non rejouable.
- ``KnowledgeBaseSettings.max_document_tokens`` (``KB_MAX_DOCUMENT_TOKENS``)
  alimente ``build_extract_version_handler`` (couvert en intégration,
  ``test_s8_document_volume``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.adapters.controller.tasks.execution import WorkContext
from knowledge_base.adapters.controller.tasks.extract_version_handler import (
    ExtractVersionWorkHandler,
)
from knowledge_base.adapters.controller.tasks.retry_policy import TerminalWorkError
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    ClaimedWorkItem,
)
from knowledge_base.domain.entities.knowledge_document import VersionStatus
from knowledge_base.domain.value_objects.structured_document import (
    Heading,
    Paragraph,
    StructuredDocument,
)
from knowledge_base.use_cases.import_steps import DOCX_MEDIA_TYPE
from knowledge_base.use_cases.record_extraction.record_extraction_command import (
    RecordExtraction,
)
from knowledge_base.use_cases.record_extraction.record_extraction_handler import (
    RecordExtractionHandler,
)
from tests.unit.knowledge_base.domain.services.support import COUNTER, policy, words
from tests.unit.knowledge_base.s8_support import TEXT_VOLUME_CODE
from tests.unit.knowledge_base.use_cases.extraction_fakes import (
    ExtractionRig,
    ExtractionUnitOfWork,
    make_rig,
)

SESSION = cast(AsyncSession, object())
FORMATS = [
    ("text/markdown", "cours.md"),
    ("text/plain", "cours.txt"),
    ("application/pdf", "cours.pdf"),
    (DOCX_MEDIA_TYPE, "cours.docx"),
]


class WordsParser:
    """Le parseur d'un format quelconque : un titre et ``count`` mots."""

    def __init__(self, count: int) -> None:
        self._count = count

    def parse(
        self, content: bytes, media_type: str, filename: str
    ) -> StructuredDocument:
        return StructuredDocument(
            blocks=(Heading(1, "Cours"), Paragraph(words(self._count)))
        )


def handler_for(
    rig: ExtractionRig, *, count: int, limit: int | None
) -> RecordExtractionHandler:
    return RecordExtractionHandler(
        unit_of_work=rig.unit_of_work,
        blob_reader=rig.blobs,
        parser=WordsParser(count),
        token_counter=COUNTER,
        policy=policy(1000),
        max_document_tokens=limit,
    )


async def extracted_volume(
    rig: ExtractionRig, command: RecordExtraction, count: int
) -> int:
    """Volume mesuré sans borne : la mesure que la borne doit reprendre."""
    prepared = await handler_for(rig, count=count, limit=None).prepare(command)
    assert prepared is not None
    return sum(unit.token_count for unit in prepared.units)


@pytest.mark.parametrize(("media_type", "filename"), FORMATS)
async def test_a_text_just_over_the_limit_is_refused_whatever_the_format(
    media_type: str, filename: str
) -> None:
    rig = make_rig()
    command = await rig.import_document(b"x", media_type=media_type, filename=filename)
    volume = await extracted_volume(rig, command, 50)

    with pytest.raises(Exception) as refused:
        await handler_for(rig, count=50, limit=volume - 1).prepare(command)

    assert TEXT_VOLUME_CODE in str(refused.value)


@pytest.mark.parametrize(("media_type", "filename"), FORMATS)
async def test_a_text_exactly_at_the_limit_is_accepted_whatever_the_format(
    media_type: str, filename: str
) -> None:
    rig = make_rig()
    command = await rig.import_document(b"x", media_type=media_type, filename=filename)
    volume = await extracted_volume(rig, command, 50)

    prepared = await handler_for(rig, count=50, limit=volume).prepare(command)

    assert prepared is not None
    assert prepared.chunks


async def test_without_a_limit_a_huge_text_is_accepted_as_before() -> None:
    rig = make_rig()
    command = await rig.import_document(b"x")

    prepared = await handler_for(rig, count=5000, limit=None).prepare(command)

    assert prepared is not None


# --- côté work queue -----------------------------------------------------------------


class Context:
    def __init__(self) -> None:
        self.finalize_calls = 0
        self.current: ExtractionUnitOfWork | None = None

    async def finalize(
        self, callback: Callable[[AsyncSession], Awaitable[None]]
    ) -> None:
        self.finalize_calls += 1


def work_handler(
    use_case: RecordExtractionHandler, ctx: Context
) -> ExtractVersionWorkHandler:
    def transaction(session: AsyncSession) -> ExtractionUnitOfWork:
        assert ctx.current is not None
        return ctx.current

    return ExtractVersionWorkHandler(use_case=use_case, transaction=transaction)


def claimed(command: RecordExtraction) -> ClaimedWorkItem:
    return ClaimedWorkItem(
        id=uuid4(),
        kind="extract_version",
        payload={
            "document_id": str(command.document_id),
            "version_id": str(command.version_id),
        },
        attempts=1,
        max_attempts=5,
        fencing_token=1,
        correlation_id=None,
    )


async def test_an_oversized_document_is_a_terminal_error_never_retried_or_written() -> (
    None
):
    rig = make_rig()
    ctx = Context()
    command = await rig.import_document(b"x")
    volume = await extracted_volume(rig, command, 50)
    use_case = handler_for(rig, count=50, limit=volume - 1)

    with pytest.raises(TerminalWorkError) as raised:
        await work_handler(use_case, ctx).execute(
            claimed(command), cast(WorkContext, ctx)
        )

    assert raised.value.code == TEXT_VOLUME_CODE
    assert raised.value.message  # dit la mesure et la borne, pas un code seul
    assert ctx.finalize_calls == 0
    assert rig.store.batches == []
    document = rig.harness.state.documents[command.document_id]
    assert document.get_version(command.version_id).status is VersionStatus.RECEIVED
