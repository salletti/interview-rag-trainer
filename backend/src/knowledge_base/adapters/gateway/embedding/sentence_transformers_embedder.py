"""Adapter ``Embedder`` sur SentenceTransformers (ADR 0008 §4, ADR 0011 §1).

Responsabilités de l'adapter, pas du domaine :

- préfixes du profil (``passage: `` à l'indexation, ``query: `` à la
  recherche), ajoutés ici et nulle part ailleurs : un préfixe oublié ou inversé
  dégrade la recherche sans erreur visible (ADR 0011, Risques) ;
- normalisation des vecteurs selon le profil ;
- ``token_counter`` : le tokenizer du modèle, sans préfixe ni tokens spéciaux,
  qui mesure le budget des chunks (ADR 0009 §3).

``sentence_transformers`` (donc torch) n'est importé que par ``_load`` :
importer ce module ne charge ni torch ni le modèle
(``tests/architecture/test_embedding_confinement.py``). L'encodage, CPU et
long, tourne dans un thread (``asyncio.to_thread``) pour laisser la boucle
envoyer les heartbeats (S0 §7).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol, Self

from knowledge_base.domain.providers.token_counter import TokenCounter
from knowledge_base.domain.value_objects.embedding_profile import EmbeddingProfile

logger = logging.getLogger(__name__)

# Le profil décrit un calcul sur CPU (ADR 0008 §6) : pas d'accélérateur choisi
# implicitement (MPS sous macOS), pour des vecteurs identiques en local et en
# conteneur.
DEVICE = "cpu"


class TextTokenizer(Protocol):
    def encode(
        self, text: str, *, add_special_tokens: bool = False
    ) -> Sequence[int]: ...


class SentenceEncoder(Protocol):
    """Ce que l'adapter utilise d'un ``SentenceTransformer``."""

    @property
    def tokenizer(self) -> TextTokenizer: ...

    def encode(
        self, sentences: Sequence[str], *, normalize_embeddings: bool
    ) -> Sequence[Sequence[float]]: ...


class SentenceTransformersEmbedder:
    def __init__(self, profile: EmbeddingProfile, model: SentenceEncoder) -> None:
        self._profile = profile
        self._model = model
        self._token_counter = _ModelTokenCounter(model)

    @classmethod
    def load(
        cls, profile: EmbeddingProfile, *, cache_dir: str | Path | None = None
    ) -> Self:
        """Charge le modèle tout de suite (tests ``model``, outils)."""
        return cls(profile, _load(profile, cache_dir))

    @classmethod
    def lazy(
        cls, profile: EmbeddingProfile, *, cache_dir: str | Path | None = None
    ) -> Self:
        """Ne charge le modèle qu'à son premier usage (encodage ou comptage de
        tokens) : le worker d'ingestion le charge une fois par processus
        enfant, à sa première tâche, jamais dans le processus maître."""
        return cls(profile, _LazyEncoder(lambda: _load(profile, cache_dir)))

    @property
    def profile(self) -> EmbeddingProfile:
        return self._profile

    @property
    def token_counter(self) -> TokenCounter:
        return self._token_counter

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        return await self._encode([self._profile.passage_prefix + t for t in texts])

    async def embed_query(self, text: str) -> list[float]:
        (vector,) = await self._encode([self._profile.query_prefix + text])
        return vector

    async def _encode(self, sentences: list[str]) -> list[list[float]]:
        vectors = await asyncio.to_thread(
            self._model.encode,
            sentences,
            normalize_embeddings=self._profile.normalize,
        )
        return [[float(x) for x in vector] for vector in vectors]


class _ModelTokenCounter:
    def __init__(self, model: SentenceEncoder) -> None:
        self._model = model

    def count(self, text: str) -> int:
        # Le tokenizer est relu à chaque appel : avec ``_LazyEncoder``, le
        # premier comptage déclenche le chargement.
        return len(self._model.tokenizer.encode(text, add_special_tokens=False))


class _LazyEncoder:
    """``SentenceEncoder`` chargé au premier usage, une seule fois même si deux
    threads le demandent ensemble."""

    def __init__(self, load: Callable[[], SentenceEncoder]) -> None:
        self._load = load
        self._loaded: SentenceEncoder | None = None
        self._lock = threading.Lock()

    @property
    def tokenizer(self) -> TextTokenizer:
        return self._model().tokenizer

    def encode(
        self, sentences: Sequence[str], *, normalize_embeddings: bool
    ) -> Sequence[Sequence[float]]:
        return self._model().encode(
            sentences, normalize_embeddings=normalize_embeddings
        )

    def _model(self) -> SentenceEncoder:
        with self._lock:
            if self._loaded is None:
                self._loaded = self._load()
            return self._loaded


class _LoadedModel:
    """Un ``SentenceTransformer`` vu comme ``SentenceEncoder`` : un lot par
    appel (la taille du lot est celle de l'appelant), sorties en listes."""

    def __init__(self, model: Any) -> None:
        self._model = model

    @property
    def tokenizer(self) -> TextTokenizer:
        tokenizer: TextTokenizer = self._model.tokenizer
        return tokenizer

    def encode(
        self, sentences: Sequence[str], *, normalize_embeddings: bool
    ) -> Sequence[Sequence[float]]:
        vectors = self._model.encode(
            list(sentences),
            batch_size=max(1, len(sentences)),
            normalize_embeddings=normalize_embeddings,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        rows: list[list[float]] = vectors.tolist()
        return rows


def _load(profile: EmbeddingProfile, cache_dir: str | Path | None) -> SentenceEncoder:
    """Télécharge au besoin (premier démarrage : volume de cache vide) puis
    charge le modèle à la révision du profil."""
    from sentence_transformers import SentenceTransformer

    started = time.perf_counter()
    model = SentenceTransformer(
        profile.model,
        revision=profile.revision,
        cache_folder=None if cache_dir is None else str(cache_dir),
        device=DEVICE,
    )
    logger.info(
        "modèle d'embeddings %s@%s chargé en %.1f s (cache %s)",
        profile.model,
        profile.revision,
        time.perf_counter() - started,
        cache_dir or "par défaut",
    )
    return _LoadedModel(model)
