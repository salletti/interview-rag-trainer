"""Compteur de tokens provisoire de S3 (ADR 0009 §3), aligné par excès sur le
tokenizer XLM-R du profil ``intfloat/multilingual-e5-base``.

Les comptes réels ci-dessous ont été mesurés le 2026-11-18 avec le vrai
tokenizer (``tokenizers``, ``add_special_tokens=False``), hors de ce dépôt :
ils figent la propriété qui compte, ne jamais sous-estimer un texte de la
taille d'un chunk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowledge_base.adapters.gateway.embedding.approximate_token_counter import (
    ApproximateTokenCounter,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "knowledge_base"
COUNTER = ApproximateTokenCounter()

# Fixture -> tokens réels XLM-R du fichier entier.
MEASURED = {
    "qa_python_fastapi_fr.md": 507,
    "qa_python_en.md": 230,
    "cours_asyncio_fr.md": 812,
    "notes_python.txt": 321,
    "fiche_revision_python_fr.md": 169,
}


@pytest.mark.parametrize(("name", "real"), MEASURED.items())
def test_the_count_never_underestimates_a_measured_document(
    name: str, real: int
) -> None:
    estimated = COUNTER.count((FIXTURES / name).read_text(encoding="utf-8"))

    assert real <= estimated <= 2 * real


def test_words_count_one_token_per_three_characters() -> None:
    assert COUNTER.count("a") == 1
    assert COUNTER.count("abc") == 1
    assert COUNTER.count("abcd") == 2
    assert COUNTER.count("un deux") == 3


def test_accented_words_and_punctuation_after_a_blank_cost_more() -> None:
    assert COUNTER.count("ete") == 1
    assert COUNTER.count("été") == 2
    assert COUNTER.count("a:") == 2
    assert COUNTER.count("a :") == 3
    assert COUNTER.count("get_db") == 3


def test_an_empty_or_blank_text_has_no_token() -> None:
    assert COUNTER.count("") == 0
    assert COUNTER.count(" \n\t") == 0


def test_the_count_grows_with_every_prefix() -> None:
    text = "def get_db(): # Règles : « yield » !\n    return db.query(Item).all()"

    counts = [COUNTER.count(text[:end]) for end in range(len(text) + 1)]

    assert counts == sorted(counts)
