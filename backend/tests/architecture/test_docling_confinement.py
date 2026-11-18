"""Aucun type Docling hors de l'adapter (ADR 0008 §2, roadmap S3 et S6).

``test_dependency_rules`` interdit déjà ``docling`` dans ``domain/`` et
``use_cases/``. Cette règle va plus loin : ``docling``, ``docling_core`` et
``pypdfium2`` (moteur PDF de Docling, S6) ne sont importés QUE par les modules
``adapters/gateway/parsing/docling_*.py`` (mapping, parseurs Markdown, PDF,
DOCX) ; ni les autres adapters (dont ``plain_text_parser`` et le routage), ni
``frameworks/``, ni ``runtime`` ne les voient.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
PARSING = Path("knowledge_base/adapters/gateway/parsing")
FORBIDDEN_ROOTS = frozenset({"docling", "docling_core", "pypdfium2"})


def allowed(relative: Path) -> bool:
    return relative.parent == PARSING and relative.name.startswith("docling_")


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
            if leaks and not allowed(relative):
                offenders.append(f"{relative}:{node.lineno}")
    return offenders


def test_docling_is_imported_only_by_its_parsing_adapters() -> None:
    assert docling_importers(SRC) == []


def test_the_rule_detects_a_leak_in_another_module(tmp_path: Path) -> None:
    leak = tmp_path / "knowledge_base" / "adapters" / "gateway" / "parsing"
    leak.mkdir(parents=True)
    (leak / "plain_text_parser.py").write_text(
        "from docling.document_converter import X\n"
    )
    (leak / "docling_markdown_parser.py").write_text("import docling_core\n")
    (leak / "docling_pdf_parser.py").write_text("import pypdfium2\n")
    (leak / "routing_document_parser.py").write_text("import pypdfium2\n")
    nested = leak / "docling_sub"
    nested.mkdir()
    (nested / "docling_x.py").write_text("import docling\n")
    domain = tmp_path / "knowledge_base" / "domain"
    domain.mkdir(parents=True)
    (domain / "leaky.py").write_text("import docling.datamodel\n")

    assert docling_importers(tmp_path) == [
        "knowledge_base/adapters/gateway/parsing/docling_sub/docling_x.py:1",
        "knowledge_base/adapters/gateway/parsing/plain_text_parser.py:1",
        "knowledge_base/adapters/gateway/parsing/routing_document_parser.py:1",
        "knowledge_base/domain/leaky.py:1",
    ]
