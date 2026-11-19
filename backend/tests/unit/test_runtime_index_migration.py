"""Commande d'exploitation ``python -m runtime.index_migration`` (S9 ; décision
1 de l'orchestrateur : pilotage par commande, pas de route HTTP).

Elle ne fait qu'analyser ses arguments et appeler la surface publique de
Knowledge Base (``start_index_migration``, ``activate_search_index``,
``abandon_index_migration``, revue S9) : la migration elle-même est vérifiée
par les tests du contexte.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

import knowledge_base
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
