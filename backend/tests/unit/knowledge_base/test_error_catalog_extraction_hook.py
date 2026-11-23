"""Le hook d'échec de ``extract_version`` écrit ``retryable`` d'après le
catalogue (S8, O14), pour TOUT code du catalogue : plus de liste parallèle
(``NOT_RETRYABLE_CODES``) qui puisse diverger.

Avant S8, le hook tenait pour rejouable tout code absent de
``NOT_RETRYABLE_CODES`` (``INVALID_PAYLOAD`` sortait « rejouable » alors que
rien n'est à rouvrir). Depuis la revue S8 (I1), ``NOT_FINALIZED`` et
``UNKNOWN_KIND`` SONT rejouables (correctif déployé) : seul le catalogue le
dit, ce test suit le catalogue.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.adapters.controller.tasks.extract_version_handler import (
    ExtractVersionWorkHandler,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    ClaimedWorkItem,
)
from knowledge_base.domain.providers.work_queue import FailureInfo
from knowledge_base.domain.value_objects.error_catalog import (
    ERROR_CATALOG,
    is_replayable,
)
from tests.unit.knowledge_base.use_cases.extraction_fakes import (
    ExtractionRig,
    ExtractionUnitOfWork,
    make_rig,
)

SESSION = cast(AsyncSession, object())
COURSE = b"# Cours asyncio\n\nLes coroutines ne bloquent pas la boucle.\n"


class Context:
    def __init__(self) -> None:
        self.current: ExtractionUnitOfWork | None = None

    async def finalize(
        self, callback: Callable[[AsyncSession], Awaitable[None]]
    ) -> None:
        raise AssertionError("le hook d'échec ne finalise jamais")


async def fail_with(
    rig: ExtractionRig, document_id: UUID, version_id: UUID, error: FailureInfo
) -> None:
    ctx = Context()

    def transaction(session: AsyncSession) -> ExtractionUnitOfWork:
        assert ctx.current is not None
        return ctx.current

    handler = ExtractVersionWorkHandler(use_case=rig.handler, transaction=transaction)
    item = ClaimedWorkItem(
        id=uuid4(),
        kind="extract_version",
        payload={"document_id": str(document_id), "version_id": str(version_id)},
        attempts=1,
        max_attempts=5,
        fencing_token=1,
        correlation_id=None,
    )
    async with rig.unit_of_work() as uow:
        ctx.current = uow
        await handler.on_terminal_failure(SESSION, item, error)
        await uow.commit()


@pytest.mark.parametrize("code", sorted(ERROR_CATALOG))
async def test_the_version_failure_is_retryable_exactly_when_the_catalog_says_so(
    code: str,
) -> None:
    rig = make_rig()
    command = await rig.import_document(COURSE)

    await fail_with(
        rig, command.document_id, command.version_id, FailureInfo(code, "diagnostic")
    )

    document = rig.harness.state.documents[command.document_id]
    failure = document.get_version(command.version_id).failure
    assert failure is not None
    assert (failure.code, failure.retryable) == (code, is_replayable(code))
