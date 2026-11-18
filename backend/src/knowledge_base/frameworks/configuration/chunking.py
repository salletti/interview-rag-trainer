"""Politique de découpage de production (ADR 0009 §7).

Budget : 512 tokens du profil multilingual-e5-base, moins 2 tokens spéciaux
(<s>, </s>) et 2 pour le préfixe « passage: » ajouté par l'Embedder (mesurés
sur le tokenizer réel) ; l'en-tête contextuel fait partie du texte compté, il
n'appelle pas de marge.

``tokenizer`` désigne le compteur qui mesure ce budget :
``approx-xlmr-v1`` est l'``ApproximateTokenCounter`` provisoire, qui
surestime. Brancher un autre compteur (le tokenizer réel en S4) impose un autre
nom : les lots déjà produits ne sont alors plus ceux de la politique courante.
"""

from __future__ import annotations

from typing import Final

from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy

DEFAULT_CHUNKING_POLICY: Final = ChunkingPolicy(
    version="kb-chunking-v1", tokenizer="approx-xlmr-v1", max_tokens=508
)
