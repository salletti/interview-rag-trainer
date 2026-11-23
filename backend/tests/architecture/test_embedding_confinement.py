"""La pile d'embeddings n'est importée que par son adapter (S4 ; ADR 0011).

``sentence_transformers``, ``transformers`` et ``torch`` ne sont importés QUE
par ``knowledge_base/adapters/gateway/embedding/sentence_transformers_embedder.py``
(et, dans ce module, seulement à l'intérieur d'une fonction : l'import du module
ne charge ni torch ni le modèle). ``test_dependency_rules`` interdit déjà ces
bibliothèques dans ``domain/`` et ``use_cases/``.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
ALLOWED = Path(
    "knowledge_base/adapters/gateway/embedding/sentence_transformers_embedder.py"
)
FORBIDDEN_ROOTS = frozenset({"sentence_transformers", "transformers", "torch"})


def importers(src: Path, *, top_level_only: bool = False) -> list[str]:
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        nodes = tree.body if top_level_only else list(ast.walk(tree))
        for node in nodes:
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            else:
                continue
            if any(m.split(".")[0] in FORBIDDEN_ROOTS for m in modules):
                offenders.append(f"{path.relative_to(src)}:{node.lineno}")
    return offenders


def test_the_embedding_stack_is_imported_only_by_the_embedder_adapter() -> None:
    leaks = [o for o in importers(SRC) if not o.startswith(str(ALLOWED))]

    assert leaks == []


def test_importing_the_embedder_module_does_not_load_the_model_stack() -> None:
    assert importers(SRC, top_level_only=True) == []


def test_the_rules_detect_a_leak_and_a_top_level_import(tmp_path: Path) -> None:
    gateway = tmp_path / "knowledge_base" / "adapters" / "gateway" / "embedding"
    gateway.mkdir(parents=True)
    (gateway / "sentence_transformers_embedder.py").write_text(
        "def load():\n    import sentence_transformers\n"
    )
    (gateway / "other.py").write_text("import torch\n")
    domain = tmp_path / "knowledge_base" / "domain"
    domain.mkdir(parents=True)
    (domain / "leaky.py").write_text("from transformers import AutoTokenizer\n")

    everywhere = importers(tmp_path)
    top_level = importers(tmp_path, top_level_only=True)

    assert sorted(everywhere) == [
        "knowledge_base/adapters/gateway/embedding/other.py:1",
        "knowledge_base/adapters/gateway/embedding/sentence_transformers_embedder.py:2",
        "knowledge_base/domain/leaky.py:1",
    ]
    assert sorted(top_level) == [
        "knowledge_base/adapters/gateway/embedding/other.py:1",
        "knowledge_base/domain/leaky.py:1",
    ]
