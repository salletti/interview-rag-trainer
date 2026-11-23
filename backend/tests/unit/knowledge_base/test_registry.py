"""Registre de handlers : kind inconnu et double enregistrement (S0 §8.2, §12.1).

API figée : ``HandlerRegistry(handlers=())``, ``register(handler)``,
``get(kind)`` (lève ``TerminalWorkError("UNKNOWN_KIND")``),
``max_attempts_by_kind()``,
``DuplicateHandlerError`` (sous-classe de ``ValueError``).
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from knowledge_base.adapters.controller.tasks.execution import WorkContext
from knowledge_base.adapters.controller.tasks.registry import (
    DuplicateHandlerError,
    HandlerRegistry,
)
from knowledge_base.adapters.controller.tasks.retry_policy import (
    TerminalWorkError,
    classify_error,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    ClaimedWorkItem,
)


class FakeExtract:
    kind: ClassVar[str] = "test.extract"
    queue: ClassVar[str] = "test.queue"
    max_attempts: ClassVar[int] = 5

    async def execute(self, item: ClaimedWorkItem, ctx: WorkContext) -> None:
        return None


class FakeIndex:
    kind: ClassVar[str] = "test.index"
    queue: ClassVar[str] = "test.queue"
    max_attempts: ClassVar[int] = 3

    async def execute(self, item: ClaimedWorkItem, ctx: WorkContext) -> None:
        return None


class AnotherExtract(FakeExtract):
    pass


def test_registry_returns_the_handler_registered_for_a_kind() -> None:
    extract, index = FakeExtract(), FakeIndex()
    registry = HandlerRegistry()
    registry.register(extract)
    registry.register(index)

    assert registry.get("test.extract") is extract
    assert registry.get("test.index") is index


def test_registry_gives_the_max_attempts_declared_by_each_handler() -> None:
    registry = HandlerRegistry([FakeExtract(), FakeIndex()])
    assert registry.max_attempts_by_kind() == {"test.extract": 5, "test.index": 3}


def test_registry_is_empty_by_default() -> None:
    assert HandlerRegistry().max_attempts_by_kind() == {}


def test_registry_unknown_kind_is_a_terminal_error_unknown_kind() -> None:
    registry = HandlerRegistry([FakeExtract()])

    with pytest.raises(TerminalWorkError) as excinfo:
        registry.get("extract_version")

    assert excinfo.value.code == "UNKNOWN_KIND"
    assert "extract_version" in str(excinfo.value)


def test_registry_unknown_kind_is_classified_as_a_failure_not_a_retry() -> None:
    with pytest.raises(TerminalWorkError) as excinfo:
        HandlerRegistry().get("nope")

    decision = classify_error(excinfo.value, attempts=1, max_attempts=5)
    assert decision.action == "fail"
    assert decision.failure is not None
    assert decision.failure.code == "UNKNOWN_KIND"


def test_registry_refuses_registering_the_same_kind_twice() -> None:
    registry = HandlerRegistry([FakeExtract()])

    with pytest.raises(DuplicateHandlerError, match=r"test\.extract"):
        registry.register(AnotherExtract())


def test_registry_keeps_the_first_handler_after_a_refused_duplicate() -> None:
    first = FakeExtract()
    registry = HandlerRegistry([first])

    with pytest.raises(DuplicateHandlerError):
        registry.register(AnotherExtract())

    assert registry.get("test.extract") is first


def test_registry_refuses_duplicates_given_to_the_constructor() -> None:
    with pytest.raises(DuplicateHandlerError):
        HandlerRegistry([FakeExtract(), AnotherExtract()])


def test_duplicate_handler_error_is_a_value_error() -> None:
    assert issubclass(DuplicateHandlerError, ValueError)
