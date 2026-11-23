"""Jeu de référence et métriques de l'évaluation de la recherche (S5).

Code de TEST (pas de ``src/``) : les métriques sont elles-mêmes vérifiées par
``tests/unit/knowledge_base/test_retrieval_metrics.py``, car une métrique fausse
validerait n'importe quelle recherche.

Définitions (ADR 0009 conditions de réévaluation ; roadmap S5) :

- une requête a UNE unité attendue, désignée par son document source
  (``source_filename`` de la citation) et par des extraits qui doivent figurer
  dans le texte du passage (``answer_snippet`` toujours) ;
- rang d'une requête : rang (à partir de 1) du premier passage qui est
  l'unité attendue, ``None`` s'il n'y en a pas dans les résultats ;
- Recall@k : part des requêtes dont le rang est <= k (une seule unité
  attendue par requête : Recall@k = taux de réussite) ;
- MRR : moyenne de 1 / rang, 0 pour une requête sans rang ;
- intégrité d'unité : un passage attendu est « intègre » s'il contient la
  question (unités QA), la réponse de référence ET l'exemple de code attendus ;
  mesurée sur les requêtes dont l'unité est retrouvée dans les 5 premiers.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "knowledge_base"
REFERENCE = FIXTURES / "retrieval" / "retrieval_reference.json"
THRESHOLDS = FIXTURES / "retrieval" / "retrieval_thresholds.json"


@dataclass(frozen=True)
class ReferenceDocument:
    file: str
    title: str
    kind: str
    language: str
    topics: tuple[str, ...]

    @property
    def filename(self) -> str:
        return Path(self.file).name

    @property
    def path(self) -> Path:
        return FIXTURES / self.file


@dataclass(frozen=True)
class Expected:
    file: str
    answer_snippet: str
    question: str | None
    code_snippet: str | None

    @property
    def filename(self) -> str:
        return Path(self.file).name


@dataclass(frozen=True)
class ReferenceFilters:
    language: str | None
    topics: tuple[str, ...]
    kinds: tuple[str, ...]


@dataclass(frozen=True)
class ReferenceQuery:
    id: str
    text: str
    language: str
    tags: tuple[str, ...]
    filters: ReferenceFilters | None
    expected: Expected

    @property
    def cross_language(self) -> bool:
        return "cross_language" in self.tags


@dataclass(frozen=True)
class Reference:
    documents: tuple[ReferenceDocument, ...]
    queries: tuple[ReferenceQuery, ...]

    def document(self, file: str) -> ReferenceDocument:
        (found,) = [d for d in self.documents if d.file == file]
        return found


def load_reference(path: Path = REFERENCE) -> Reference:
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    documents = tuple(
        ReferenceDocument(
            file=d["file"],
            title=d["title"],
            kind=d["kind"],
            language=d["language"],
            topics=tuple(d["topics"]),
        )
        for d in raw["documents"]
    )
    queries = tuple(
        ReferenceQuery(
            id=q["id"],
            text=q["text"],
            language=q["language"],
            tags=tuple(q["tags"]),
            filters=_filters(q["filters"]),
            expected=Expected(**q["expected"]),
        )
        for q in raw["queries"]
    )
    return Reference(documents, queries)


def _filters(raw: dict[str, Any] | None) -> ReferenceFilters | None:
    if raw is None:
        return None
    return ReferenceFilters(
        language=raw.get("language"),
        topics=tuple(raw.get("topics", ())),
        kinds=tuple(raw.get("kinds", ())),
    )


# --- appariement -------------------------------------------------------------------


def normalize(text: str) -> str:
    """Espaces réduits, accents graves de code en ligne retirés : le rendu de
    Docling peut reformater un paragraphe sans en changer les mots."""
    return re.sub(r"\s+", " ", text.replace("`", "")).strip()


def is_expected_unit(filename: str, text: str, expected: Expected) -> bool:
    """Le passage vient du bon document ET porte la réponse de référence."""
    return filename == expected.filename and (
        normalize(expected.answer_snippet) in normalize(text)
    )


def unit_is_intact(text: str, expected: Expected) -> bool:
    """Question (si l'unité en a une), réponse de référence et code attendu."""
    wanted = [expected.answer_snippet]
    if expected.question is not None:
        wanted.append(expected.question)
    if expected.code_snippet is not None:
        wanted.append(expected.code_snippet)
    haystack = normalize(text)
    return all(normalize(part) in haystack for part in wanted)


# --- métriques -----------------------------------------------------------------------


def rank_of_expected(
    results: Sequence[tuple[str, str]], expected: Expected
) -> int | None:
    """``results`` : ``(source_filename, text)`` dans l'ordre des passages."""
    for position, (filename, text) in enumerate(results, start=1):
        if is_expected_unit(filename, text, expected):
            return position
    return None


def recall_at_k(ranks: Sequence[int | None], k: int) -> float:
    if not ranks:
        raise ValueError("aucune requête : Recall indéfini")
    return sum(1 for rank in ranks if rank is not None and rank <= k) / len(ranks)


def mean_reciprocal_rank(ranks: Sequence[int | None]) -> float:
    if not ranks:
        raise ValueError("aucune requête : MRR indéfini")
    return sum(0.0 if rank is None else 1 / rank for rank in ranks) / len(ranks)


def unit_integrity(found: Sequence[tuple[Expected, str | None]]) -> float | None:
    """Part des unités attendues retrouvées dont le passage est intègre.
    ``found`` : ``(attendu, texte du passage attendu ou None)`` ; ``None`` si
    aucune unité n'est retrouvée (intégrité indéfinie)."""
    retrieved = [(e, t) for e, t in found if t is not None]
    if not retrieved:
        return None
    return sum(1 for e, t in retrieved if unit_is_intact(t, e)) / len(retrieved)
