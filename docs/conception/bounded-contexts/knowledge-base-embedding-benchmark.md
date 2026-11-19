# Knowledge Base — Benchmark des profils d'embeddings (S9)

> **Statut : mesure de référence S9 (2026-11-19).** Fonde l'[ADR 0013](../../decisions/0013-definitive-embedding-profile.md)
> (Proposed). Roadmap : [S9](knowledge-base-roadmap.md#s9--benchmark-des-embeddings-et-migration-bluegreen) ;
> conception : [knowledge-base.md §13, §16 O1, O4, O6](knowledge-base.md#16-points-ouverts).

## 1. Protocole

- **Script versionné** : `backend/scripts/benchmark_embeddings.py`
  (`make benchmark-embeddings`, ou `uv run python scripts/benchmark_embeddings.py [nom …]`
  depuis `backend/`). Chaque candidat est mesuré dans un **processus neuf**.
  Rapport JSON complet (rang de chaque requête) : `backend/.cache/benchmark/embedding_benchmark.json`
  (ignoré par git).
- **Pipeline** : celui de la production jusqu'à l'encodage : parseurs de
  production (Docling ; les documents sont parsés une fois par le processus
  parent pour que les modèles de layout de Docling ne pèsent pas dans la RSS
  d'un candidat), `VersionExtractor` (segmentation, découpage), politique
  `kb-chunking-v1` à **508 tokens comptés par le tokenizer de chaque
  candidat**, `SentenceTransformersEmbedder` (préfixes, normalisation, CPU,
  float32). Recherche en mémoire : produit scalaire exact (Qdrant fait de même
  sous son seuil d'indexation HNSW, 10 000 points), regroupement par unité
  (3 chunks au plus, score = meilleur chunk), filtres sur les métadonnées,
  10 passages reconstruits par `assemble_passage` (`max_passage_tokens` = 1 024).
- **Validation du protocole** : pour `multilingual-e5-base`, le script
  reproduit exactement les mesures du pipeline complet (PostgreSQL, Qdrant)
  des tests d'évaluation de S5 et S6 : S5 Recall@1 0,810, Recall@5 0,881,
  MRR 0,853, inter-langues 0,750, filtres 0,975 ; S6 office Recall@5 0,905 ;
  unités longues trouvées 1,000, intégrité tronquée 0,875 (rapports
  `backend/.cache/retrieval/*.json`, identiques à 10⁻³ près).
- **Jeux** : S5 (`retrieval/retrieval_reference.json`, 12 documents, 42
  requêtes, dont 12 inter-langues et 40 avec filtres) ; S6 office (même jeu,
  `qa_pytest_fr` en PDF et `cours_git_fr` en DOCX) ; S6 unités longues
  (`office/long_units_reference.json`, 5 documents, 10 requêtes, dont 8 sur
  une section PDF de ~1 300 tokens, 4 à identifiant de code, 5 à négatif
  proche) ; **complément S9** ajouté au jeu
  (`retrieval/benchmark_s9_queries.json`, corpus de S5, 18 requêtes) :
  12 requêtes réduites à un **identifiant de code exact**
  (`dependency_overrides`, `selectinload`, `PYTHONASYNCIODEBUG`,
  `git cherry-pick`, `RUN uv sync --frozen`…) et 6 à **négatif proche**
  (une autre unité du corpus traite la même notion).
- **Métriques** : définitions de `tests/integration/knowledge_base/retrieval_support.py`
  (Recall@k, MRR, intégrité d'unité), unité longue reconnue à sa provenance
  (S6). Recherche **hybride** (O6) : BM25 sur le texte des chunks
  (identifiants gardés entiers), fusionné au classement dense par Reciprocal
  Rank Fusion (k = 60) au niveau des unités ; mesurée seulement.
- **Ressources** : RSS du processus (`ps`) après import de la pile, après
  chargement, après encodage du corpus (61 chunks distincts, lots de 32) ;
  pic (`ru_maxrss`) ; encodage des passages (médiane par chunk) ; encodage
  d'une requête (médiane et p95 sur 112 requêtes, la première exclue) ;
  poids sur disque.

## 2. Machine et versions

Mesure du 2026-11-19 09:51 UTC : Apple M5 Pro (18 cœurs logiques, 48 Gio),
macOS 26.5.2 arm64, Python 3.13.14, torch 2.15.0 (CPU, 4 threads),
sentence-transformers 6.3.0. Hôte, pas conteneur : en S4, l'encodage était
1,8 fois plus lent dans le conteneur Linux arm64 (91 contre 51 ms/chunk).

| Candidat | Révision épinglée | Dimension | Longueur max. | Préfixes requête / passage |
|---|---|---|---|---|
| `intfloat/multilingual-e5-small` | `614241f622f53c4eeff9890bdc4f31cfecc418b3` | 384 | 512 | `query: ` / `passage: ` |
| `intfloat/multilingual-e5-base` (actuel) | `d128750597153bb5987e10b1c3493a34e5a4502a` | 768 | 512 | `query: ` / `passage: ` |
| `BAAI/bge-m3` (vecteur dense) | `5617a9f61b028005a4858fdac845db406aefb181` | 1 024 | 8 192 | aucun |
| `Qwen/Qwen3-Embedding-0.6B` | `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3` | 1 024 | 32 768 | invite « query » du modèle / aucun |

Téléchargements dans `backend/.cache/models` (aucune installation sur l'hôte,
aucune dépendance ajoutée). bge-m3 : 3 275 s de téléchargement (2,27 Go à
~0,7 Mo/s) ; sa première mesure n'a pas été lancée le 2026-11-18 à cause d'une
boucle d'attente fautive du script de pilotage (la ligne de fin de
téléchargement cherchée dans les 300 derniers caractères du journal en avait
été chassée par la ligne de Qwen), pas d'un échec du modèle ; relancée le
2026-11-19, elle a abouti en 35 s.

## 3. Résultats

Recherche dense (celle de la production). « Hyb. » : recherche hybride
BM25 + dense (O6).

| | e5-small | **e5-base** | bge-m3 | Qwen3-0.6B |
|---|---|---|---|---|
| Poids sur disque (Mio) | 449 | 1 061 | 2 166 | 1 136 |
| RSS après import de la pile (Mio) | 535 | 535 | 535 | 534 |
| RSS après chargement (Mio) | 979 | 980 | 981 | 2 961 |
| RSS après encodage / pic (Mio) | 1 718 / 1 721 | 2 579 / 2 584 | 3 793 / 3 795 | 4 363 / 4 365 |
| Chargement, cache chaud (s) | 0,80 | 0,81 | 0,81 | 0,79 |
| Encodage d'un passage (ms/chunk, médiane, lots de 32) | 28,2 | 84,0 | 248,7 | 297,5 |
| Encodage d'une requête, médiane / p95 (ms) | 5,4 / 6,0 | 13,4 / 17,1 | 37,7 / 44,5 | 98,7 / 116,9 |
| **S5** Recall@1 | 0,762 | 0,810 | 0,881 | 0,952 |
| S5 Recall@5 | 0,857 | 0,881 | 0,976 | 0,976 |
| S5 MRR | 0,798 | 0,853 | 0,913 | 0,958 |
| S5 intégrité d'unité | 1,000 | 1,000 | 1,000 | 1,000 |
| S5 inter-langues Recall@5 (12 requêtes) | 0,667 | 0,750 | 1,000 | 1,000 |
| S5 même langue Recall@5 (30) | 0,933 | 0,933 | 0,967 | 0,967 |
| S5 avec filtres / mêmes requêtes sans (40) | 0,950 / 0,850 | 0,975 / 0,875 | 1,000 / 0,975 | 1,000 / 0,975 |
| **S6 office** (PDF + DOCX) Recall@5 / MRR | 0,857 / 0,799 | 0,905 / 0,857 | 0,976 / 0,898 | 0,976 / 0,946 |
| **S6 unités longues** Recall@5 / MRR | 0,900 / 0,800 | 0,900 / 0,833 | 0,800 / 0,800 | 1,000 / 0,883 |
| unité longue dans les 5 premiers | 1,000 | 1,000 | 1,000 | 1,000 |
| intégrité des passages tronqués | 0,875 | 0,875 | 0,750 | 1,000 |
| identifiants de code Recall@5 (4) | 1,000 | 1,000 | 0,750 | 1,000 |
| négatifs proches Recall@1 (5) | 0,400 | 0,800 | 1,000 | 1,000 |
| **S9 identifiants de code** Recall@1 / @5 (12) | 0,833 / 1,000 | 0,833 / 1,000 | 0,833 / 0,833 | 0,917 / 0,917 |
| S9 identifiants, hybride Recall@1 / @5 | 0,917 / 1,000 | 1,000 / 1,000 | 0,917 / 1,000 | 0,917 / 1,000 |
| S9 négatifs proches Recall@1 (6) | 1,000 | 1,000 | 1,000 | 1,000 |
| S5 hybride Recall@5 / inter-langues | 0,833 / 0,500 | 0,857 / 0,500 | 0,905 / 0,667 | 0,929 / 0,750 |

Lecture :

- **Qualité** : Qwen3-0.6B est premier ou ex aequo sur chaque jeu ; sur S5, il
  retrouve 4 requêtes de plus qu'e5-base dans les 5 premiers et 6 de plus en
  première position (sur 42) ; il rend les 12 requêtes inter-langues. bge-m3
  égale Qwen3 en Recall@5 sur S5 et S6 office, mais **régresse** sur les unités
  longues (Recall@5 0,800, intégrité tronquée 0,750) et sur les identifiants de
  code (0,833 et 0,750). e5-small est en retrait partout (inter-langues 0,667,
  négatifs proches des unités longues 0,400).
- **Coût** : par rapport à e5-base, Qwen3 encode un passage 3,5 fois plus
  lentement et une requête 7,4 fois plus lentement, et matérialise ses poids
  dès le chargement (2 961 Mio de RSS, contre 980 pour les modèles dont les
  poids `safetensors` ne sont lus qu'au premier encodage) ; pic 4 365 Mio par
  processus. bge-m3 : 3 fois plus lent par passage, pic 3 795 Mio.
- **float32** : Qwen3 se charge par défaut en bfloat16 (sa configuration) ;
  sur ce CPU, le bfloat16 encode un passage de 100 mots en 477 à 517 ms contre
  61 à 141 ms en float32 (lots de 1 à 32, mesure isolée du 2026-11-18), et la
  première mesure du benchmark en bfloat16 donnait 3 672 ms/chunk. Le chargeur
  de production impose désormais `dtype=float32`
  (`sentence_transformers_embedder._load`) ; sans effet pour E5, déjà en
  float32. Toutes les valeurs du tableau sont en float32.
- **Limites du jeu** : 42 + 28 + 18 requêtes, 61 chunks ; une requête vaut
  0,024 sur S5. Le jeu S9 est facile pour la recherche dense (e5-base : 18/18
  dans les 5 premiers) : il ne départage pas les candidats sur les négatifs
  proches.

## 4. Contraintes d'exploitation mesurées

- La VM Docker de la stack de dev dispose de **7,75 Gio** au total
  (`docker info`, 2026-11-19) pour PostgreSQL, Qdrant, RabbitMQ, l'API, deux
  workers, le scheduler et le frontend.
- Le modèle de l'index actif est chargé par **deux** processus (API pour les
  requêtes, `worker_ingestion` pour les passages) ; pendant une migration,
  chacun peut porter deux modèles (démonstration, §5). Avec Qwen3 (2 961 à
  4 365 Mio par processus sur l'hôte), le seul modèle actif occuperait de
  5,8 à 8,5 Gio dans ces deux processus : il ne tient pas dans la VM de dev,
  a fortiori pendant une migration. *Déduction des mesures par processus,
  non exécutée dans la VM.*
- Le worker d'ingestion compte les tokens de l'extraction avec le tokenizer
  du profil initial : changer de profil de production sans changer la
  politique d'extraction ferait re-parser chaque nouveau document (rechunking)
  et garder deux modèles dans le worker.

## 5. Démonstration blue/green sur la stack de dev (2026-11-18 et 2026-11-19)

Corpus : 15 versions publiées (jeu S5, plus un PDF et un DOCX ; 56 points).
Boucle de requêtes REST continues (5 requêtes en rotation, `limit` 5)
pendant chaque migration.

| | Aller : e5-base → **e5-small** | Retour : e5-small → **e5-base** |
|---|---|---|
| `make kb-migration-start target=…` | 2,2 s ; 15 versions, **15 à rechunker** (autre tokenizer) | 15 versions, 0 à rechunker (lots d'origine) |
| Backfill complet (rechunking, indexation, synchronisation) | 53 s (19:58:31 → 19:59:24 UTC) | 41 s (09:52:41 → 09:53:22 UTC) |
| `make kb-migration-activate index=…` | 1,3 s, accepté | accepté |
| Recherches pendant la migration | **6 190, 0 erreur** (HTTP 200 seulement), une seule bascule de l'ancien index au nouveau, jamais de retour, 5 passages à chaque réponse | **6 467, 0 erreur**, une seule bascule, jamais de retour, 5 passages à chaque réponse |
| Première requête sur le nouvel index | 2 781 ms (chargement du modèle par l'API) | 2 176 ms (idem) |
| Latence REST médiane / p95, ancien → nouvel index | 60,6 / 95,7 ms → 62,8 / 94,0 ms | 64,9 / 96,1 ms → 74,3 / 104,1 ms |
| Collection de l'ancien index supprimée | 301,7 s après l'activation (délai de 300 s) | 301,4 s après l'activation |
| RSS API (processus serveur) avant / après bascule (deux modèles) | 1 277 → 1 666 Mio | 1 001 → 1 666 Mio |
| RSS `worker_ingestion` (enfant) pendant le backfill | 1 964 Mio (e5-base pour l'extraction + e5-small) | 1 353 Mio (pic 1 425 ; enfant remplacé par `--max-tasks-per-child`, e5-base seul) |

RSS lue dans `/proc/<pid>/status` (`VmRSS`, `VmHWM`) des processus des
conteneurs. Le processus serveur de l'API garde les deux modèles après la
bascule (aucun déchargement en V1) ; 1 001 Mio avant le retour : l'API venait
d'être rechargée (`--reload` du mode dev), e5-small seul chargé.

La stack de dev est laissée sur `multilingual-e5-base`, profil recommandé.

## 6. Recherche hybride (O6)

Mesurée sur les quatre modèles (§3) : la fusion BM25 + dense ne gagne qu'en
première position sur les identifiants de code (e5-base : Recall@1 0,833 →
1,000 sur 12 requêtes ; Recall@5 déjà à 1,000) et **dégrade** les requêtes
inter-langues pour tous les modèles (e5-base : 0,750 → 0,500 ; Qwen3 : 1,000
→ 0,750), donc le Recall@5 de S5 (e5-base : 0,881 → 0,857). Décision proposée
(§16, O6) : **report**, réexaminé avec un corpus où la recherche dense manque
des identifiants dans les 5 premiers.
