"""Compteur de tokens provisoire (S3). Depuis S4, la production compte avec
le tokenizer réel du profil (``SentenceTransformersEmbedder.token_counter``,
ADR 0009 §3) ; ce compteur, sans dépendance ni modèle, ne sert plus qu'aux
tests de coût du ``ChunkPlanner``.

Le profil initial, ``intfloat/multilingual-e5-base``, utilise le tokenizer
SentencePiece de XLM-R. Le tirer en S3 imposerait le téléchargement du modèle ;
ce compteur l'approche **par excès** sans aucune dépendance :

- un mot (lettres et chiffres) compte ``ceil(longueur / 3)`` tokens, plus un
  s'il contient un caractère non ASCII (accents) ;
- ``_`` et chaque signe de ponctuation comptent un token, deux s'ils suivent
  un blanc (XLM-R émet alors un ``▁`` isolé).

Mesure du 2026-11-18 contre le vrai tokenizer (``tokenizers`` dans un conteneur
jetable), sur les fixtures du corpus, trois documents de conception et trois
modules Python du dépôt, découpés en 937 paragraphes et lignes de code :
estimation / réel = 1,40 au global ; jamais inférieure au réel sur les
échantillons de 50 tokens réels ou plus (pire rapport 1,07) ; sous-estimation
sur 9 échantillons très courts (pire 0,83 pour « Règles : », 4 contre 5), sans
conséquence loin du budget. Le compte croît avec le texte : la découpe aux
caractères du ``ChunkPlanner`` en dépend.
"""

from __future__ import annotations

import math
import re
from typing import Final

CHARACTERS_PER_TOKEN: Final = 3
_PIECE: Final = re.compile(r"(\s*)([^\W_]+|[^\w\s]|_)")


class ApproximateTokenCounter:
    def count(self, text: str) -> int:
        tokens = 0
        for space, piece in _PIECE.findall(text):
            if piece[0].isalnum():
                tokens += max(1, math.ceil(len(piece) / CHARACTERS_PER_TOKEN))
                tokens += 0 if piece.isascii() else 1
            else:
                tokens += 2 if space else 1
        return tokens
