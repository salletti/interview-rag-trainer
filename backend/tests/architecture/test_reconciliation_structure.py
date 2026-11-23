"""Structure de S8 (réconciliation, rejeu, catalogue d'erreurs ; roadmap S8,
005).

1. Les use cases de S8 existent selon la forme des autres (``<nom>_command.py``
   et ``<nom>_handler.py`` dans ``use_cases/<nom>/``) ; le handler de work item
   et son routeur REST vivent dans ``adapters/controller/`` ; aucun dossier hors
   de ce qu'utilise la slice.
2. Les use cases de S8 ne touchent pas le système de fichiers : le nettoyage des
   temporaires et des orphelins passe par des ports (``StagedUploadStore``,
   ``DocumentBlobEraser``).
3. Les règles de décision de la réconciliation sont dans le domaine, pures :
   ``domain/services/reconciliation.py`` n'importe ni framework, ni adapter, ni
   entrée-sortie (``test_dependency_rules`` en vérifie déjà les frameworks ;
   ici, ni système de fichiers, ni réseau, ni horloge).
4. Le catalogue des codes d'erreur est UNIQUE : un code n'est défini (constante
   de module ``NOM = "CODE"``) que dans ``error_catalog.py`` ; les autres
   modules le réexportent ou l'importent. Sans cette règle, deux listes
   divergent (incohérence ``BLOB_MISSING`` de S3, O14).
5. Chaque ``TerminalWorkError`` du code source reçoit un code du catalogue
   (revue S8, M5) : constante du catalogue, ou code d'une
   ``DocumentParseError`` (``error.code.value``, liste fermée dont chaque
   membre est au catalogue). La construction le vérifie aussi à l'exécution.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from knowledge_base.domain.value_objects.error_catalog import ERROR_CATALOG

SRC = Path(__file__).resolve().parents[2] / "src"
KB = SRC / "knowledge_base"
S8_USE_CASES = (
    "reconcile_knowledge_base",
    "retry_ingestion",
    "list_failures",
    "clean_stale_uploads",
)
FORBIDDEN_IN_RULES = frozenset(
    {"os", "shutil", "pathlib", "glob", "tempfile", "socket", "time", "datetime"}
)
CATALOG = KB / "domain" / "value_objects" / "error_catalog.py"


def imported_modules(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.lineno, node.module))
    return found


def code_definitions(src: Path, codes: set[str], *, allowed: Path) -> list[str]:
    """Constantes de module ``NOM = "CODE"`` (ou ``NOM: Final = "CODE"``) dont la
    valeur est un code du catalogue, hors du catalogue lui-même."""
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        if path == allowed or "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            value = node.value if isinstance(node, ast.Assign | ast.AnnAssign) else None
            if (
                isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and value.value in codes
            ):
                offenders.append(f"{path.relative_to(src)}:{node.lineno} {value.value}")
    return offenders


@pytest.mark.parametrize("name", S8_USE_CASES)
def test_each_s8_use_case_has_its_command_and_its_handler(name: str) -> None:
    package = KB / "use_cases" / name

    assert (package / "__init__.py").is_file()
    assert (package / f"{name}_command.py").is_file()
    assert (package / f"{name}_handler.py").is_file()


def test_the_work_item_handler_and_the_router_are_controller_adapters() -> None:
    controller = KB / "adapters" / "controller"

    assert (controller / "tasks" / "reconcile_handler.py").is_file()
    assert (controller / "rest" / "failures_router.py").is_file()


@pytest.mark.parametrize("name", S8_USE_CASES)
def test_the_s8_use_cases_do_not_touch_the_filesystem(name: str) -> None:
    package = KB / "use_cases" / name

    assert package.is_dir(), f"use_cases/{name} est absent"
    offenders = [
        f"{path.name}:{line} {module}"
        for path in sorted(package.rglob("*.py"))
        for line, module in imported_modules(path)
        if module.split(".")[0] in {"os", "shutil", "pathlib", "glob", "tempfile"}
    ]
    assert offenders == []


def test_the_reconciliation_rules_are_pure() -> None:
    rules = KB / "domain" / "services" / "reconciliation.py"

    assert rules.is_file()
    offenders = [
        f"{line} {module}"
        for line, module in imported_modules(rules)
        if module.split(".")[0] in FORBIDDEN_IN_RULES
    ]
    assert offenders == []


def test_an_error_code_is_defined_once_in_the_catalog() -> None:
    assert CATALOG.is_file()

    assert code_definitions(KB, set(ERROR_CATALOG), allowed=CATALOG) == []


def test_the_catalog_rule_detects_a_second_definition(tmp_path: Path) -> None:
    (tmp_path / "catalog.py").write_text('BLOB_MISSING = "BLOB_MISSING"\n')
    (tmp_path / "dup.py").write_text(
        'from typing import Final\nX: Final = "BLOB_MISSING"\n'
    )
    (tmp_path / "ok.py").write_text(
        "from catalog import BLOB_MISSING\nY = BLOB_MISSING\nZ = 'autre'\n"
    )

    found = code_definitions(
        tmp_path, {"BLOB_MISSING"}, allowed=tmp_path / "catalog.py"
    )

    assert found == ["dup.py:2 BLOB_MISSING"]


def terminal_error_codes(src: Path) -> list[tuple[str, str]]:
    """Premier argument de chaque ``TerminalWorkError(...)`` : nom de constante,
    littéral, ou expression (rendue telle quelle)."""
    found: list[tuple[str, str]] = []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "TerminalWorkError"
                and node.args
            ):
                argument = node.args[0]
                if isinstance(argument, ast.Constant):
                    code = str(argument.value)
                else:
                    code = ast.unparse(argument)
                found.append((f"{path.relative_to(src)}:{node.lineno}", code))
    return found


def test_every_terminal_work_error_carries_a_catalog_code() -> None:
    from knowledge_base.domain.exceptions.parsing_errors import ParseErrorCode

    parse_error_code = "error.code.value"
    uses = terminal_error_codes(KB)

    assert uses, "aucune TerminalWorkError trouvée : la règle ne vérifie rien"
    offenders = [
        f"{where} {code}"
        for where, code in uses
        if code not in ERROR_CATALOG and code != parse_error_code
    ]
    assert offenders == []
    assert {code.value for code in ParseErrorCode} <= set(ERROR_CATALOG)
