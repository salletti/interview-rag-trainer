"""Structure de S7 (retrait, purge, orphelins ; roadmap S7, ADR 0012, 005).

1. Les use cases de S7 existent selon la forme des autres (``<nom>_command.py``
   et ``<nom>_handler.py`` dans ``use_cases/<nom>/``), et le handler de work
   item ``purge_document`` vit dans ``adapters/controller/tasks/`` : aucun
   dossier, aucune abstraction hors de ce qu'utilise la slice.
2. La suppression de fichiers (``unlink``, ``rmtree``, ``rmdir``, ``removedirs``,
   ``os.remove``) n'existe QUE dans ``adapters/gateway/blob/`` : la purge et le
   nettoyage des orphelins passent par le port ``DocumentBlobEraser``, jamais
   par le système de fichiers. Règle verte aujourd'hui (seul l'adapter de blob
   supprime un fichier), elle protège le port contre un raccourci.
3. Les use cases de S7 n'importent aucun module du système de fichiers.

``test_dependency_rules`` interdit déjà les frameworks (dont ``qdrant_client``)
dans ``use_cases/`` : la suppression des points ne peut passer que par
``VectorIndex``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
KB = SRC / "knowledge_base"
BLOB_ADAPTER = Path("knowledge_base/adapters/gateway/blob")
S7_USE_CASES = ("withdraw_document", "purge_document", "clean_blob_orphans")
DELETING_ATTRIBUTES = frozenset({"unlink", "rmtree", "rmdir", "removedirs"})
FILESYSTEM_MODULES = frozenset({"os", "shutil", "pathlib", "glob", "tempfile"})


def deletions_outside_the_blob_adapter(src: Path) -> list[str]:
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        relative = path.relative_to(src)
        if "__pycache__" in path.parts or relative.parent == BLOB_ADAPTER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            ):
                continue
            attribute, owner = node.func.attr, node.func.value
            is_os_remove = (
                attribute == "remove"
                and isinstance(owner, ast.Name)
                and owner.id == "os"
            )
            if attribute in DELETING_ATTRIBUTES or is_os_remove:
                offenders.append(f"{relative}:{node.lineno} {attribute}")
    return offenders


def filesystem_imports(directory: Path) -> list[str]:
    offenders: list[str] = []
    for path in sorted(directory.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            else:
                continue
            offenders.extend(
                f"{path.name}:{node.lineno} {module}"
                for module in modules
                if module.split(".")[0] in FILESYSTEM_MODULES
            )
    return offenders


@pytest.mark.parametrize("name", S7_USE_CASES)
def test_each_s7_use_case_has_its_command_and_its_handler(name: str) -> None:
    package = KB / "use_cases" / name

    assert (package / "__init__.py").is_file()
    assert (package / f"{name}_command.py").is_file()
    assert (package / f"{name}_handler.py").is_file()


def test_the_purge_work_item_handler_is_an_adapter_of_the_tasks_package() -> None:
    handler = KB / "adapters" / "controller" / "tasks" / "purge_document_handler.py"

    assert handler.is_file()


def test_only_the_blob_adapter_deletes_files() -> None:
    assert deletions_outside_the_blob_adapter(SRC) == []


@pytest.mark.parametrize("name", S7_USE_CASES)
def test_the_s7_use_cases_do_not_touch_the_filesystem(name: str) -> None:
    package = KB / "use_cases" / name

    assert package.is_dir(), f"use_cases/{name} est absent"
    assert filesystem_imports(package) == []


def test_the_deletion_rule_detects_a_leak_and_spares_the_blob_adapter(
    tmp_path: Path,
) -> None:
    blob = tmp_path / BLOB_ADAPTER
    blob.mkdir(parents=True)
    (blob / "store.py").write_text("from pathlib import Path\nPath('x').unlink()\n")
    leak = tmp_path / "knowledge_base" / "use_cases" / "purge_document"
    leak.mkdir(parents=True)
    (leak / "a.py").write_text("import shutil\nshutil.rmtree('x')\n")
    (leak / "b.py").write_text("import os\nos.remove('x')\n")
    (leak / "c.py").write_text("from pathlib import Path\nPath('x').unlink()\n")
    (leak / "d.py").write_text("items = []\nitems.remove(1)\n")  # liste : sans objet

    found = deletions_outside_the_blob_adapter(tmp_path)

    assert sorted(found) == [
        "knowledge_base/use_cases/purge_document/a.py:2 rmtree",
        "knowledge_base/use_cases/purge_document/b.py:2 remove",
        "knowledge_base/use_cases/purge_document/c.py:2 unlink",
    ]


def test_the_filesystem_rule_detects_an_import(tmp_path: Path) -> None:
    (tmp_path / "handler.py").write_text("import os.path\nfrom pathlib import Path\n")
    (tmp_path / "ok.py").write_text("import logging\n")

    assert filesystem_imports(tmp_path) == [
        "handler.py:1 os.path",
        "handler.py:2 pathlib",
    ]
