"""Adapter ``SentenceTransformersEmbedder`` : préfixes de modèle, normalisation,
tokenizer (S4 ; ADR 0011 §1 et Risques : « préfixes E5 oubliés ou inversés :
dégradation silencieuse, couverte par des tests unitaires de l'adapter »).

Le modèle sous-jacent est un double (``FakeEncoder``) qui enregistre les textes
reçus : aucune dépendance ``sentence_transformers`` n'est requise.

API figée (``knowledge_base.adapters.gateway.embedding.
sentence_transformers_embedder``) :

- ``SentenceTransformersEmbedder(profile: EmbeddingProfile, model:
  SentenceEncoder)`` ; le module n'importe ``sentence_transformers`` qu'à
  l'intérieur de la fabrique ``SentenceTransformersEmbedder.load(profile, *,
  cache_dir: str | None = None)`` (non testée ici : modèle réel, marqueur
  ``model``).
- ``SentenceEncoder`` (Protocol) : ``encode(sentences: Sequence[str], *,
  normalize_embeddings: bool) -> Sequence[Sequence[float]]`` et ``tokenizer``
  exposant ``encode(text: str, *, add_special_tokens: bool = False) ->
  Sequence[int]``.
- ``profile`` ; ``await embed_passages(texts) -> list[list[float]]`` : envoie
  ``profile.passage_prefix + texte`` ; ``await embed_query(text) ->
  list[float]`` : envoie ``profile.query_prefix + texte`` ;
  ``normalize_embeddings=profile.normalize`` ; sorties en ``list[float]`` dans
  l'ordre des entrées ; liste vide -> liste vide sans appel au modèle.
- ``token_counter`` : un ``TokenCounter`` (``count(text) -> int``) basé sur le
  tokenizer du modèle, SANS préfixe ni tokens spéciaux.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from knowledge_base.adapters.gateway.embedding.sentence_transformers_embedder import (
    SentenceTransformersEmbedder,
)
from knowledge_base.domain.value_objects.embedding_profile import (
    MULTILINGUAL_E5_BASE,
    EmbeddingProfile,
)


class FakeTokenizer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def encode(self, text: str, *, add_special_tokens: bool = False) -> Sequence[int]:
        self.calls.append((text, add_special_tokens))
        return [len(word) for word in text.split()]


class FakeEncoder:
    def __init__(self) -> None:
        self.sentences: list[list[str]] = []
        self.normalize_flags: list[bool] = []
        self.tokenizer = FakeTokenizer()

    def encode(
        self, sentences: Sequence[str], *, normalize_embeddings: bool
    ) -> list[list[float]]:
        self.sentences.append(list(sentences))
        self.normalize_flags.append(normalize_embeddings)
        return [[float(len(s)), 1.0, 0.5] for s in sentences]


def make(
    profile: EmbeddingProfile = MULTILINGUAL_E5_BASE,
) -> tuple[SentenceTransformersEmbedder, FakeEncoder]:
    encoder = FakeEncoder()
    return SentenceTransformersEmbedder(profile=profile, model=encoder), encoder


async def test_passages_are_sent_with_the_passage_prefix() -> None:
    embedder, encoder = make()

    await embedder.embed_passages(["Depends injecte une dépendance.", "Autre texte."])

    assert encoder.sentences == [
        ["passage: Depends injecte une dépendance.", "passage: Autre texte."]
    ]


async def test_a_query_is_sent_with_the_query_prefix() -> None:
    embedder, encoder = make()

    await embedder.embed_query("Qu'est-ce que Depends ?")

    assert encoder.sentences == [["query: Qu'est-ce que Depends ?"]]


async def test_prefixes_come_from_the_profile_not_from_the_code() -> None:
    profile = replace(MULTILINGUAL_E5_BASE, query_prefix="Q> ", passage_prefix="P> ")
    embedder, encoder = make(profile)

    await embedder.embed_passages(["a"])
    await embedder.embed_query("b")

    assert encoder.sentences == [["P> a"], ["Q> b"]]


async def test_the_prefixes_are_not_swapped_between_passages_and_queries() -> None:
    embedder, encoder = make()

    await embedder.embed_passages(["x"])
    await embedder.embed_query("x")

    passage, query = (batch[0] for batch in encoder.sentences)
    assert passage.startswith("passage: ") and not passage.startswith("query: ")
    assert query.startswith("query: ") and not query.startswith("passage: ")


async def test_the_original_texts_are_not_altered_beyond_the_prefix() -> None:
    embedder, encoder = make()
    text = "  Texte avec espaces et accents é à  \n"

    await embedder.embed_passages([text])

    assert encoder.sentences == [["passage: " + text]]


async def test_normalization_follows_the_profile() -> None:
    normalized, encoder_on = make()
    raw, encoder_off = make(replace(MULTILINGUAL_E5_BASE, normalize=False))

    await normalized.embed_passages(["a"])
    await normalized.embed_query("a")
    await raw.embed_passages(["a"])

    assert encoder_on.normalize_flags == [True, True]
    assert encoder_off.normalize_flags == [False]


async def test_vectors_are_plain_float_lists_in_input_order() -> None:
    embedder, _ = make()

    vectors = await embedder.embed_passages(["a", "bcd"])

    assert vectors == [
        [float(len("passage: a")), 1.0, 0.5],
        [float(len("passage: bcd")), 1.0, 0.5],
    ]
    assert all(type(x) is float for vector in vectors for x in vector)


async def test_a_query_vector_is_a_single_flat_list() -> None:
    embedder, _ = make()

    vector = await embedder.embed_query("abc")

    assert vector == [float(len("query: abc")), 1.0, 0.5]


async def test_no_passage_means_no_model_call() -> None:
    embedder, encoder = make()

    assert await embedder.embed_passages([]) == []
    assert encoder.sentences == []


def test_the_profile_is_exposed() -> None:
    embedder, _ = make()

    assert embedder.profile == MULTILINGUAL_E5_BASE


def test_the_token_counter_uses_the_model_tokenizer_without_prefix_or_specials() -> (
    None
):
    embedder, encoder = make()

    count = embedder.token_counter.count("un deux trois")

    assert count == 3
    assert encoder.tokenizer.calls == [("un deux trois", False)]
