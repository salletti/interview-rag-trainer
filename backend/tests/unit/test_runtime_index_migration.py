"""Commande d'exploitation ``python -m runtime.index_migration`` (S9 ; décision
1 de l'orchestrateur : pilotage par commande, pas de route HTTP).

Elle ne fait qu'analyser ses arguments et appeler la surface publique de
Knowledge Base (``start_index_migration``, ``activate_search_index``,
``abandon_index_migration``, revue S9) : la migration elle-même est vérifiée
par les tests du contexte.

Un refus d'opération (``knowledge_base.IndexOperationRefusedError``, défaut D2
de la validation globale) sort en code 1 avec un message d'opérateur d'une
ligne sur stderr, sans trace ; une erreur inattendue garde sa trace.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest

import knowledge_base
from knowledge_base.domain.entities.knowledge_corpus import IndexStatus
from knowledge_base.domain.exceptions.corpus_errors import InvalidIndexTransitionError
from runtime import index_migration

INDEX = UUID(int=42)


def test_only_catalogued_targets_are_accepted() -> None:
    with pytest.raises(SystemExit):
        index_migration.parser().parse_args(["start", "modele-inconnu"])

    parsed = index_migration.parser().parse_args(["start", "multilingual-e5-base"])
    assert parsed.target == "multilingual-e5-base"


def test_activation_needs_an_index_identifier() -> None:
    with pytest.raises(SystemExit):
        index_migration.parser().parse_args(["activate", "pas-un-uuid"])

    parsed = index_migration.parser().parse_args(["activate", str(INDEX)])
    assert parsed.index_id == INDEX


def test_start_prints_the_building_index(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    async def start(_sessions: Any, _qdrant: Any, target: str) -> UUID:
        calls.append(target)
        return INDEX

    monkeypatch.setattr(knowledge_base, "start_index_migration", start)

    index_migration.main(["start", "multilingual-e5-small"])

    assert calls == ["multilingual-e5-small"]
    assert capsys.readouterr().out.strip() == str(INDEX)


def test_activate_calls_the_public_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[UUID] = []

    async def activate(_sessions: Any, index_id: UUID) -> None:
        calls.append(index_id)

    monkeypatch.setattr(knowledge_base, "activate_search_index", activate)

    index_migration.main(["activate", str(INDEX)])

    assert calls == [INDEX]


def test_abandon_needs_an_index_identifier() -> None:
    with pytest.raises(SystemExit):
        index_migration.parser().parse_args(["abandon", "pas-un-uuid"])

    parsed = index_migration.parser().parse_args(["abandon", str(INDEX)])
    assert parsed.action == "abandon"
    assert parsed.index_id == INDEX


def test_abandon_calls_the_public_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[UUID] = []

    async def abandon(_sessions: Any, index_id: UUID) -> None:
        calls.append(index_id)

    monkeypatch.setattr(knowledge_base, "abandon_index_migration", abandon)

    index_migration.main(["abandon", str(INDEX)])

    assert calls == [INDEX]


def refusing(error: Exception) -> Callable[..., Any]:
    async def operation(*_args: Any) -> None:
        raise error

    return operation


@pytest.mark.parametrize(
    ("action", "entry_point", "argument"),
    [
        ("start", "start_index_migration", "multilingual-e5-small"),
        ("activate", "activate_search_index", str(INDEX)),
        ("abandon", "abandon_index_migration", str(INDEX)),
    ],
)
def test_a_refusal_is_a_one_line_operator_message_and_exit_code_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
    entry_point: str,
    argument: str,
) -> None:
    refusal = knowledge_base.IndexOperationRefusedError("migration déjà en cours")
    monkeypatch.setattr(knowledge_base, entry_point, refusing(refusal))

    with pytest.raises(SystemExit) as exited:
        index_migration.main([action, argument])

    assert exited.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "refusé : migration déjà en cours\n"


def test_the_refusal_of_an_already_retired_index_has_no_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reproduction de D2 : second ``abandon`` d'un index ``RETIRED``."""
    refusal = InvalidIndexTransitionError(INDEX, IndexStatus.RETIRED, "retrait")
    monkeypatch.setattr(knowledge_base, "abandon_index_migration", refusing(refusal))

    with pytest.raises(SystemExit) as exited:
        index_migration.main(["abandon", str(INDEX)])

    assert exited.value.code == 1
    err = capsys.readouterr().err
    assert err == f"refusé : index {INDEX} : retrait interdit depuis RETIRED\n"
    assert "Traceback" not in err


def test_an_unexpected_error_keeps_its_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_base,
        "activate_search_index",
        refusing(RuntimeError("base injoignable")),
    )

    with pytest.raises(RuntimeError, match="base injoignable"):
        index_migration.main(["activate", str(INDEX)])
