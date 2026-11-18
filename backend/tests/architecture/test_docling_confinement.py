"""Aucun type Docling hors de l'adapter (ADR 0008 §2, roadmap S3).

``test_dependency_rules`` interdit déjà ``docling`` dans ``domain/`` et
``use_cases/``. Cette règle va plus loin : ``docling`` et ``docling_core`` ne
sont importés QUE par ``adapters/gateway/parsing/docling_markdown_parser.py`` ;
ni les autres adapters (dont ``plain_text_parser``), ni ``frameworks/``, ni
``runtime`` ne les voient.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
ALLOWED = Path("knowledge_base/adapters/gateway/parsing/docling_markdown_parser.py")
FORBIDDEN_ROOTS = frozenset({"docling", "docling_core"})


def docling_importers(src: Path) -> list[str]:
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            else:
                continue
            leaks = any(m.split(".")[0] in FORBIDDEN_ROOTS for m in modules)
            relative = path.relative_to(src)
            if leaks and relative != ALLOWED:
                offenders.append(f"{relative}:{node.lineno}")
    return offenders


def test_docling_is_imported_only_by_its_markdown_adapter() -> None:
    assert docling_importers(SRC) == []


def test_the_rule_detects_a_leak_in_another_module(tmp_path: Path) -> None:
    leak = tmp_path / "knowledge_base" / "adapters" / "gateway" / "parsing"
    leak.mkdir(parents=True)
    (leak / "plain_text_parser.py").write_text(
        "from docling.document_converter import X\n"
    )
    (leak / "docling_markdown_parser.py").write_text("import docling_core\n")
    domain = tmp_path / "knowledge_base" / "domain"
    domain.mkdir(parents=True)
    (domain / "leaky.py").write_text("import docling.datamodel\n")

    assert docling_importers(tmp_path) == [
        "knowledge_base/adapters/gateway/parsing/plain_text_parser.py:1",
        "knowledge_base/domain/leaky.py:1",
    ]
