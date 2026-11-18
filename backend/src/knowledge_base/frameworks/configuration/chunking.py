"""Politique de découpage de production (ADR 0009 §7).

Budget : 512 tokens du profil multilingual-e5-base, moins 2 tokens spéciaux
(<s>, </s>) et 2 pour le préfixe « passage: » ajouté par l'Embedder (mesurés
sur le tokenizer réel) ; l'en-tête contextuel fait partie du texte compté, il
n'appelle pas de marge.

``tokenizer`` désigne le compteur qui mesure ce budget : depuis S4, le
tokenizer XLM-R du profil, à sa révision épinglée (``xlmr-<révision courte>``,
fourni par ``SentenceTransformersEmbedder.token_counter``). Le compteur
provisoire de S3 (``approx-xlmr-v1``) produisait d'autres lots : changer de
compteur change l'identité de la politique, donc celle des unités et chunks.
"""

from __future__ import annotations

from typing import Final

from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy
from knowledge_base.domain.value_objects.embedding_profile import MULTILINGUAL_E5_BASE

# Tokens spéciaux (2) et préfixe ``passage: `` (2), hors du texte compté.
_RESERVED_TOKENS: Final = 4

DEFAULT_CHUNKING_POLICY: Final = ChunkingPolicy(
    version="kb-chunking-v1",
    tokenizer=MULTILINGUAL_E5_BASE.tokenizer,
    max_tokens=MULTILINGUAL_E5_BASE.max_tokens - _RESERVED_TOKENS,
)
