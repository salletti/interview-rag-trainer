# ADR 0013 — Profil d'embeddings définitif de Knowledge Base

- **Date :** 2026-11-19
- **Statut :** Proposed (à accepter par l'utilisateur).
- **Complète** l'[ADR 0011](0011-embedding-profile-search-index-blue-green.md) et,
  une fois accepté, **remplace son point 6 pour le seul choix du profil** ;
  l'ADR 0011 n'est pas modifié (lien réciproque dans sa section Suivi).

## Contexte

L'ADR 0011 a choisi `intfloat/multilingual-e5-base` sans mesure locale, en
annonçant un nouvel ADR fondé sur le benchmark de S9. Le benchmark
([rapport](../conception/bounded-contexts/knowledge-base-embedding-benchmark.md))
compare quatre candidats sur le pipeline de production (jeux S5, S6 et
complément S9 : 88 requêtes) et mesure leur coût CPU et mémoire ; la migration
blue/green a été démontrée sur la stack de dev. Contraintes : exécution sur
CPU (ADR 0008 §6), usage mono-utilisateur, modèle chargé par l'API **et** par
`worker_ingestion`, deux modèles par processus pendant une migration, VM
Docker de dev de 7,75 Gio pour toute la stack.

## Décision

1. **Profil définitif V1 : `intfloat/multilingual-e5-base`**, révision
   `d128750597153bb5987e10b1c3493a34e5a4502a`, 768 dimensions, cosine,
   vecteurs normalisés, préfixes `query: ` / `passage: `, 512 tokens,
   tokenizer `xlmr-d1287505` ; politique `kb-chunking-v1+xlmr-d1287505`,
   508 tokens. Aucune migration de l'index de production n'est nécessaire.
2. **Critère retenu** : le meilleur profil dont le modèle actif et un second
   modèle de transition tiennent dans le budget mémoire de la stack
   (API + worker) avec Docling. Mesuré : e5-base, pic 2,6 Gio par processus ;
   Qwen3-Embedding-0.6B 4,4 Gio et bge-m3 3,8 Gio par processus, hors budget
   dans une VM de 7,75 Gio.
3. **Cibles de migration du catalogue** (`embedding_targets.CATALOGUE`) :
   `multilingual-e5-base` et `multilingual-e5-small` (repli léger, migration
   démontrée aller-retour). Qwen3-Embedding-0.6B et bge-m3 n'y entrent pas.
4. Le chargeur de production impose **float32** sur CPU (Qwen3 déclare
   bfloat16, 3 à 8 fois plus lent sur ce CPU).

## Alternatives envisagées

- **Qwen3-Embedding-0.6B (float32)** : meilleure qualité mesurée (S5
  Recall@1 0,952 contre 0,810, Recall@5 0,976 contre 0,881, inter-langues
  1,000 contre 0,750, intégrité des passages tronqués 1,000 contre 0,875),
  mais passages 3,5 fois et requêtes 7,4 fois plus lents (297 ms/chunk,
  99 ms/requête sur l'hôte) et 2,96 à 4,37 Gio par processus. **Chemin de
  montée en gamme** dès que le budget mémoire le permet.
- **bge-m3** : Recall@5 de S5 0,976, mais régresse sur les unités longues
  (0,800, intégrité tronquée 0,750) et les identifiants de code (0,833) ; pic
  3,8 Gio, 2,2 Go de poids.
- **multilingual-e5-small** : 3 fois plus rapide, ~0,9 Gio de moins au pic,
  mais en retrait partout (S5 Recall@5 0,857, inter-langues 0,667).
- **Recherche hybride BM25 + dense** (O6) : reportée ; elle dégrade
  l'inter-langues pour tous les modèles mesurés.

## Conséquences

- Pas de changement de profil en production ; l'index initial reste valide.
- Le catalogue et la commande `python -m runtime.index_migration` permettent
  une migration future sans interruption (mesures : §5 du rapport).
- La politique d'extraction reste celle du profil initial : une migration
  vers un autre tokenizer ajoute un rechunking par nouveau document tant
  qu'elle n'est pas alignée (point à reprendre avec la montée en gamme).

## Risques et compromis

- Jeu d'évaluation petit (une requête = 0,024 sur S5) : l'écart de qualité
  avec Qwen3 est net mais mesuré sur peu de requêtes.
- La déduction « Qwen3 ne tient pas dans la VM » vient des mesures par
  processus sur l'hôte, pas d'une exécution dans la VM.

## Conditions de réévaluation

- Budget mémoire de la stack ≥ ~10 Gio (Qwen3 : deux processus au pic, plus
  un second modèle pendant une migration) : refaire le benchmark dans le
  conteneur et proposer la migration vers Qwen3 (alignement de la politique
  d'extraction compris).
- Jeu d'évaluation étendu (plus d'unités longues, plus de requêtes
  inter-langues) ou régression d'e5-base sur un nouveau corpus.
- Accélérateur disponible (GPU), qui changerait le coût mesuré.

## Liens

- [ADR 0011](0011-embedding-profile-search-index-blue-green.md) (mécanisme,
  profil initial), [ADR 0008](0008-custom-pipeline-docling-extraction.md) (CPU),
  [ADR 0009](0009-pedagogical-chunking-knowledge-units.md) (politique).
- [Rapport de benchmark](../conception/bounded-contexts/knowledge-base-embedding-benchmark.md),
  [knowledge-base.md §13, §16](../conception/bounded-contexts/knowledge-base.md#16-points-ouverts).
