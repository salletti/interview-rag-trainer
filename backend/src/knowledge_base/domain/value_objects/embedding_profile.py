"""Profil d'embeddings versionné (knowledge-base.md §2, §13 ; ADR 0011 §1, §6).

Un ``SearchIndex`` porte exactement un profil : deux profils distincts ne
partagent jamais une collection, car leurs vecteurs ne sont pas comparables.
Le domaine ne fait que décrire le modèle ; l'adapter d'embeddings l'applique
(préfixes, normalisation, tokenizer).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Distance(StrEnum):
    COSINE = "COSINE"
    DOT = "DOT"
    EUCLID = "EUCLID"


@dataclass(frozen=True)
class EmbeddingProfile:
    model: str
    # Commit du dépôt du modèle : sans révision épinglée, un même nom peut
    # désigner d'autres poids d'un téléchargement à l'autre.
    revision: str
    dimension: int
    distance: Distance
    normalize: bool
    query_prefix: str
    passage_prefix: str
    max_tokens: int
    # Nom du tokenizer qui mesure le budget des chunks ; il entre dans
    # l'identité de la ``ChunkingPolicy`` (sans ``+``).
    tokenizer: str

    def __post_init__(self) -> None:
        for name in ("model", "revision", "tokenizer"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} du profil d'embeddings vide")
        if self.dimension <= 0:
            raise ValueError(f"dimension doit être positive : {self.dimension}")
        if self.max_tokens <= 0:
            raise ValueError(f"max_tokens doit être positif : {self.max_tokens}")


MULTILINGUAL_E5_REVISION: Final = "d128750597153bb5987e10b1c3493a34e5a4502a"

# Profil initial, révisable par un nouvel ADR après le benchmark de S9
# (ADR 0011 §6). Révision relevée sur le Hub le 2026-11-18. Le tokenizer est
# celui de XLM-R (SentencePiece) livré avec le modèle à cette révision.
MULTILINGUAL_E5_BASE: Final = EmbeddingProfile(
    model="intfloat/multilingual-e5-base",
    revision=MULTILINGUAL_E5_REVISION,
    dimension=768,
    distance=Distance.COSINE,
    normalize=True,
    query_prefix="query: ",
    passage_prefix="passage: ",
    max_tokens=512,
    tokenizer=f"xlmr-{MULTILINGUAL_E5_REVISION[:8]}",
)


MULTILINGUAL_E5_SMALL_REVISION: Final = "614241f622f53c4eeff9890bdc4f31cfecc418b3"

# Cible de migration de repli (S9 ; ADR 0013 Proposed, point 3) : même famille
# que l'index initial (XLM-R, préfixes « query: »/« passage: », 512 tokens),
# 384 dimensions. Son tokenizer est compté sous sa propre identité :
# ``tokenizer.json`` diffère de celui de multilingual-e5-base à sa révision
# (empreintes relevées le 2026-11-18), même si le vocabulaire SentencePiece est
# identique ; le même budget peut donc découper autrement.
MULTILINGUAL_E5_SMALL: Final = EmbeddingProfile(
    model="intfloat/multilingual-e5-small",
    revision=MULTILINGUAL_E5_SMALL_REVISION,
    dimension=384,
    distance=Distance.COSINE,
    normalize=True,
    query_prefix="query: ",
    passage_prefix="passage: ",
    max_tokens=512,
    tokenizer=f"xlmr-{MULTILINGUAL_E5_SMALL_REVISION[:8]}",
)
