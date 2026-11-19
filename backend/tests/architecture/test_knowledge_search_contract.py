"""Contrat public ``KnowledgeSearch`` de Knowledge Base (S5 ; knowledge-base.md
§14 ; 005, « Surface publique d'un contexte » ; ADR 0007).

Vérifié par analyse statique des imports (``ast``), comme les autres règles :

- ``KnowledgeSearch`` vit dans ``contracts/providers/knowledge_search.py`` et
  les DTO dans ``contracts/result/`` ; les noms et champs sont ceux du §14 ;
- ``contracts/`` n'importe que la bibliothèque standard et lui-même : un autre
  contexte le consomme sans rien tirer de l'intérieur du contexte (domaine,
  cas d'usage, adapters, frameworks, Qdrant, SQLAlchemy, Docling) ;
- ``domain/`` et ``use_cases/`` n'importent jamais leur propre façade
  ``contracts`` : seul l'adapter ``adapters/controller/contracts/`` (et les
  routes qui servent le contrat) la traduisent ;
- la racine ``knowledge_base`` exporte ``build_knowledge_search`` ;
  ``runtime`` ne la câble que par cette racine.

Limite connue : l'import de ``knowledge_base.contracts`` exécute d'abord
``knowledge_base/__init__.py`` (qui réexporte ``adapters.controller``) ; la
garantie testée est donc l'absence de dépendance DÉCLARÉE du contrat, pas celle
du chargement du package racine.
"""

from __future__ import annotations

import ast
import dataclasses
import sys
import typing
from pathlib import Path

import pytest

from tests.architecture.test_dependency_rules import (
    SRC,
    contexts_never_import_each_other_internals,
    imported_modules,
    python_files,
    runtime_only_sees_public_surface,
)

KB = SRC / "knowledge_base"
CONTRACTS = KB / "contracts"
OWN = "knowledge_base.contracts"


def contract_leaks(src: Path) -> list[str]:
    """Imports de ``knowledge_base/contracts`` hors bibliothèque standard et
    hors ``knowledge_base.contracts`` lui-même."""
    return [
        f"{path.relative_to(src)} importe {module}"
        for path in python_files(src / "knowledge_base" / "contracts")
        for module in imported_modules(path, src)
        if module.split(".")[0] not in sys.stdlib_module_names
        and not (module == OWN or module.startswith(f"{OWN}."))
    ]


# --- emplacement et forme -----------------------------------------------------


def test_the_protocol_lives_in_contracts_providers() -> None:
    assert (CONTRACTS / "providers" / "knowledge_search.py").is_file()
    assert (CONTRACTS / "result").is_dir()


def test_the_protocol_has_a_single_async_search_method() -> None:
    from knowledge_base.contracts.providers import KnowledgeSearch
    from knowledge_base.contracts.result import (
        KnowledgeQuery,
        KnowledgeSearchResult,
    )

    assert typing.is_protocol(KnowledgeSearch)
    members = {
        name
        for name in vars(KnowledgeSearch)
        if not name.startswith("_") and callable(vars(KnowledgeSearch)[name])
    }
    assert members == {"search"}
    hints = typing.get_type_hints(KnowledgeSearch.search)
    assert hints["query"] is KnowledgeQuery
    assert hints["return"] is KnowledgeSearchResult


def test_the_protocol_is_reexported_by_the_contracts_facade() -> None:
    from knowledge_base import contracts
    from knowledge_base.contracts.providers import KnowledgeSearch

    assert contracts.KnowledgeSearch is KnowledgeSearch


EXPECTED_FIELDS = {
    "KnowledgeQuery": [
        "text",
        "limit",
        "language",
        "topics",
        "kinds",
        "document_ids",
    ],
    "KnowledgeSearchResult": ["passages", "complete", "index_id"],
    "RetrievedPassage": [
        "unit_id",
        "unit_type",
        "question",
        "text",
        "truncated",
        "matched_chunk_ids",
        "score",
        "citation",
    ],
    "Citation": [
        "document_id",
        "document_title",
        "version_id",
        "version_number",
        "section_path",
        "page_start",
        "page_end",
        "source_filename",
    ],
}


@pytest.mark.parametrize("name", EXPECTED_FIELDS)
def test_each_result_type_is_a_frozen_dataclass_with_the_fields_of_the_design(
    name: str,
) -> None:
    from knowledge_base.contracts import result

    cls = getattr(result, name)

    assert dataclasses.is_dataclass(cls)
    assert typing.cast(typing.Any, cls).__dataclass_params__.frozen is True
    assert [f.name for f in dataclasses.fields(cls)] == EXPECTED_FIELDS[name]


def test_the_query_defaults_are_those_of_the_design() -> None:
    from knowledge_base.contracts.result import KnowledgeQuery

    query = KnowledgeQuery(text="depends")

    assert query.limit == 5
    assert query.language is None
    assert query.topics == ()
    assert query.kinds == ()
    assert query.document_ids == ()


# --- indépendance du contrat --------------------------------------------------


def test_contracts_only_import_the_standard_library_and_themselves() -> None:
    assert contract_leaks(SRC) == []


def test_contracts_are_scanned_and_not_empty() -> None:
    """Sans cela la règle précédente passerait « à vide »."""
    assert len(list(python_files(CONTRACTS))) >= 3  # __init__, providers, result


def test_the_contracts_rule_detects_an_interior_import(tmp_path: Path) -> None:
    fake = tmp_path / "knowledge_base" / "contracts" / "result"
    fake.mkdir(parents=True)
    (fake / "leak.py").write_text(
        "from knowledge_base.domain.value_objects.citation import Citation\n"
        "import qdrant_client\n"
        "from knowledge_base.contracts.result import Citation as Own\n"
        "from dataclasses import dataclass\n"
    )

    leaks = contract_leaks(tmp_path)

    assert any("knowledge_base.domain" in leak for leak in leaks)
    assert any("qdrant_client" in leak for leak in leaks)
    assert not any(
        "dataclasses" in leak or "contracts.result" in leak for leak in leaks
    )


def test_another_context_can_consume_the_contract_without_touching_the_interior(
    tmp_path: Path,
) -> None:
    consumer = tmp_path / "src" / "assessment" / "adapters" / "gateway" / "kb.py"
    consumer.parent.mkdir(parents=True)
    consumer.write_text(
        "from knowledge_base.contracts.providers import KnowledgeSearch\n"
        "from knowledge_base.contracts.result import KnowledgeQuery, Citation\n"
    )
    for pkg in ("runtime", "shared", "assessment", "interview_training"):
        (tmp_path / "src" / pkg).mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / pkg / "__init__.py").touch()
    (tmp_path / "src" / "knowledge_base").mkdir(exist_ok=True)

    assert contexts_never_import_each_other_internals(tmp_path / "src") == []


# --- la façade n'est pas importée de l'intérieur ------------------------------


def test_domain_and_use_cases_never_import_the_contracts_facade() -> None:
    offenders = [
        f"{path.relative_to(SRC)} importe {module}"
        for layer in ("domain", "use_cases")
        for path in python_files(KB / layer)
        for module in imported_modules(path, SRC)
        if module == OWN or module.startswith(f"{OWN}.")
    ]

    assert offenders == []


def test_the_use_case_package_for_the_search_exists() -> None:
    """Le cas d'usage a son dossier (``use_cases/search_knowledge``) : la règle
    précédente porte alors sur du code réel."""
    assert any(python_files(KB / "use_cases" / "search_knowledge"))


def test_the_contract_adapter_lives_under_adapters_controller_contracts() -> None:
    assert any(python_files(KB / "adapters" / "controller" / "contracts"))


# --- surface publique ---------------------------------------------------------


def test_the_root_package_exports_the_factory_in_its_public_surface() -> None:
    tree = ast.parse((KB / "__init__.py").read_text(encoding="utf-8"))
    exported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            exported = {
                str(elt.value)
                for elt in typing.cast(ast.List, node.value).elts
                if isinstance(elt, ast.Constant)
            }

    assert "build_knowledge_search" in exported


def test_runtime_wires_the_search_only_through_the_public_surface() -> None:
    assert runtime_only_sees_public_surface(SRC) == []
