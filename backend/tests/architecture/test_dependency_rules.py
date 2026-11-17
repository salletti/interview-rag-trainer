"""Règles de dépendance DDD / Clean Architecture, vérifiées par analyse
statique des imports (``ast``), sans exécuter le code.

Les Bounded Contexts n'ont pas encore de code : les règles tournent « à vide »
sur ``backend/src``. Pour prouver qu'elles détectent bien une violation,
chaque règle est aussi exécutée sur un faux arbre de sources contenant la
violation correspondante (``test_rules_detect_*``).

Références : docs/conception/005-project-structure.md, ADR 0003, ADR 0006.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
RUNTIME = "runtime"
SHARED = "shared"

# Bibliothèques d'infrastructure interdites dans ``domain/`` et ``use_cases/``.
FRAMEWORKS = frozenset(
    {
        "fastapi",
        "starlette",
        "sqlalchemy",
        "asyncpg",
        "alembic",
        "celery",
        "kombu",
        "pika",
        "aio_pika",
        "qdrant_client",
        "litellm",
        "pydantic_settings",
        "httpx",
        "uvicorn",
    }
)
INTERNAL_LAYERS = ("domain", "use_cases", "adapters", "frameworks")

Rule = Callable[[Path], list[str]]


# --- analyse ----------------------------------------------------------------


def bounded_contexts(src: Path) -> list[str]:
    return sorted(
        p.name
        for p in src.iterdir()
        if p.is_dir() and p.name not in (RUNTIME, SHARED) and not p.name.startswith(".")
    )


def python_files(root: Path) -> Iterator[Path]:
    yield from sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def imported_modules(path: Path, src: Path) -> set[str]:
    """Modules importés par ``path``, imports relatifs résolus en absolu."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = list(path.relative_to(src).with_suffix("").parts[:-1])
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                anchor = package[: len(package) - (node.level - 1)]
                base = ".".join([*anchor, *([node.module] if node.module else [])])
            if base:
                modules.add(base)
            # ``from .pkg import sub`` peut désigner un sous-module.
            modules.update(
                f"{base}.{alias.name}" if base else alias.name for alias in node.names
            )
    return modules


def _top(module: str) -> str:
    return module.split(".")[0]


def _layer_of(module: str) -> str | None:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else None


Predicate = Callable[[str, str], bool]


def _violations(src: Path, roots: list[Path], predicate: Predicate) -> list[str]:
    """``predicate(owner, module)`` reçoit le package racine du fichier et un
    module importé ; vrai = violation."""
    return [
        f"{path.relative_to(src)} importe {module}"
        for root in roots
        for path in python_files(root)
        for module in imported_modules(path, src)
        if predicate(root.relative_to(src).parts[0], module)
    ]


# --- règles -----------------------------------------------------------------


def domain_and_use_cases_are_framework_free(src: Path) -> list[str]:
    roots = [
        src / ctx / layer
        for ctx in [*bounded_contexts(src), SHARED]
        for layer in ("domain", "use_cases")
    ]
    return _violations(src, roots, lambda _o, m: _top(m) in FRAMEWORKS)


def domain_does_not_depend_on_outer_layers(src: Path) -> list[str]:
    roots = [src / ctx / "domain" for ctx in bounded_contexts(src)]
    return _violations(
        src,
        roots,
        lambda o, m: (
            _top(m) == o and _layer_of(m) in ("use_cases", "adapters", "frameworks")
        ),
    )


def use_cases_do_not_depend_on_adapters(src: Path) -> list[str]:
    roots = [src / ctx / "use_cases" for ctx in bounded_contexts(src)]
    return _violations(
        src,
        roots,
        lambda o, m: _top(m) == o and _layer_of(m) in ("adapters", "frameworks"),
    )


def contexts_never_import_each_other_internals(src: Path) -> list[str]:
    """Seule la façade ``<contexte>.contracts`` est importable d'un autre
    contexte (docs/conception/005-project-structure.md § Dépendances)."""
    contexts = bounded_contexts(src)
    roots = [src / ctx for ctx in contexts]
    return _violations(
        src,
        roots,
        lambda o, m: (
            _top(m) in contexts and _top(m) != o and _layer_of(m) != "contracts"
        ),
    )


def no_context_depends_on_runtime(src: Path) -> list[str]:
    roots = [src / ctx for ctx in [*bounded_contexts(src), SHARED]]
    return _violations(src, roots, lambda _o, m: _top(m) == RUNTIME)


def runtime_only_sees_public_surface(src: Path) -> list[str]:
    """Le composition root câble les contextes par leur package racine ou
    leur façade ``contracts``, jamais par leurs couches internes."""
    contexts = bounded_contexts(src)
    return _violations(
        src,
        [src / RUNTIME],
        lambda _o, m: _top(m) in contexts and _layer_of(m) in INTERNAL_LAYERS,
    )


def shared_is_a_leaf(src: Path) -> list[str]:
    forbidden = [*bounded_contexts(src), RUNTIME]
    return _violations(src, [src / SHARED], lambda _o, m: _top(m) in forbidden)


RULES: dict[str, Rule] = {
    "domain_and_use_cases_are_framework_free": domain_and_use_cases_are_framework_free,
    "domain_does_not_depend_on_outer_layers": domain_does_not_depend_on_outer_layers,
    "use_cases_do_not_depend_on_adapters": use_cases_do_not_depend_on_adapters,
    "contexts_never_import_each_other_internals": (
        contexts_never_import_each_other_internals
    ),
    "no_context_depends_on_runtime": no_context_depends_on_runtime,
    "runtime_only_sees_public_surface": runtime_only_sees_public_surface,
    "shared_is_a_leaf": shared_is_a_leaf,
}


# --- le vrai code source ------------------------------------------------------


def test_src_contains_the_validated_contexts() -> None:
    assert bounded_contexts(SRC) == ["assessment", "interview_training"]


@pytest.mark.parametrize("rule", RULES.values(), ids=list(RULES))
def test_source_tree_respects_rule(rule: Rule) -> None:
    assert rule(SRC) == []


# --- preuve que chaque règle détecte une violation -----------------------------


def _write(src: Path, relative: str, content: str = "") -> None:
    target = src / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


@pytest.fixture
def fake_src(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    for pkg in ("runtime", "shared", "assessment", "interview_training"):
        _write(src, f"{pkg}/__init__.py")
    return src


# (règle, fichier fautif, contenu, fragment attendu dans la violation)
VIOLATIONS = [
    (
        "domain_and_use_cases_are_framework_free",
        "assessment/domain/entities.py",
        "from sqlalchemy import Column\n",
        "sqlalchemy",
    ),
    (
        "domain_and_use_cases_are_framework_free",
        "assessment/use_cases/publish.py",
        "import fastapi\n",
        "fastapi",
    ),
    (
        "domain_does_not_depend_on_outer_layers",
        "assessment/domain/entities.py",
        "from ..adapters import repo\n",  # import relatif
        "assessment.adapters",
    ),
    (
        "use_cases_do_not_depend_on_adapters",
        "assessment/use_cases/publish.py",
        "from assessment.adapters.gateway import repo\n",
        "assessment.adapters.gateway",
    ),
    (
        "contexts_never_import_each_other_internals",
        "interview_training/use_cases/submit.py",
        "from assessment.domain.entities import Assessment\n",
        "assessment.domain.entities",
    ),
    (
        "no_context_depends_on_runtime",
        "assessment/adapters/rest.py",
        "from runtime.settings import get_settings\n",
        "runtime.settings",
    ),
    (
        "runtime_only_sees_public_surface",
        "runtime/app.py",
        "from assessment.adapters.rest import router\n",
        "assessment.adapters.rest",
    ),
    (
        "shared_is_a_leaf",
        "shared/domain/ids.py",
        "import assessment\n",
        "assessment",
    ),
]


@pytest.mark.parametrize(
    ("rule_name", "relative", "content", "fragment"),
    VIOLATIONS,
    ids=[f"{v[0]}:{v[1]}" for v in VIOLATIONS],
)
def test_rules_detect_a_violation(
    fake_src: Path, rule_name: str, relative: str, content: str, fragment: str
) -> None:
    _write(fake_src, relative, content)

    violations = RULES[rule_name](fake_src)

    # Un seul fichier fautif ; le nom importé peut être rapporté aussi comme
    # sous-module potentiel, d'où « au moins une » violation, toutes sur lui.
    assert violations
    assert all(fragment in violation for violation in violations)


def test_contracts_facade_is_allowed_between_contexts(fake_src: Path) -> None:
    _write(
        fake_src,
        "interview_training/adapters/acl.py",
        "from assessment.contracts import AssessmentSummary\n",
    )
    assert contexts_never_import_each_other_internals(fake_src) == []


def test_relative_imports_are_resolved_against_the_file_package(fake_src: Path) -> None:
    _write(
        fake_src,
        "assessment/domain/entities/assessment.py",
        "from . import rubric\nfrom .. import events\nfrom ...contracts import dto\n",
    )
    target = fake_src / "assessment/domain/entities/assessment.py"
    modules = imported_modules(target, fake_src)
    assert {
        "assessment.domain.entities",
        "assessment.domain.entities.rubric",
        "assessment.domain",
        "assessment.domain.events",
        "assessment.contracts",
        "assessment.contracts.dto",
    } <= modules
