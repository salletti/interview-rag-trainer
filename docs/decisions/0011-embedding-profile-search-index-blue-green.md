# ADR 0011 — EmbeddingProfile, SearchIndex et migration blue/green

- **Date :** 2026-11-17
- **Statut :** Accepted pour le mécanisme ; profil d'embeddings **initial et
  révisable** ; barrière d'activation **Proposed** jusqu'à son implémentation
  en S9.

## Contexte

Changer de modèle d'embeddings, de dimension, de préfixes ou de politique de
chunking produit des vecteurs incompatibles avec les précédents. Mélanger des
vecteurs de profils différents rend la recherche incohérente. Le profil
initial doit être choisi sans benchmark local : il doit donc rester révisable.

## Décision

1. **`EmbeddingProfile` versionné** (Value Object) : modèle, révision,
   dimension, distance, normalisation, préfixes query/passage, longueur
   maximale, tokenizer.
2. **`SearchIndex`** = un `EmbeddingProfile` + une `ChunkingPolicy`
   (ADR 0009) + une collection Qdrant dédiée. **Jamais deux profils dans une
   même collection.** États `BUILDING`, `ACTIVE`, `RETIRED`.
3. **Une seule génération `ACTIVE` par corpus en V1** ; le corpus est un
   singleton (`KnowledgeCorpus`, Aggregate Root qui porte cet invariant).
4. **Migration blue/green** : `ProvisionSearchIndex` crée un index `BUILDING`
   et planifie le backfill du corpus éligible ; la recherche reste sur l'index
   `ACTIVE` ; `ActivateSearchIndex` bascule quand la couverture est complète ;
   l'ancien index passe `RETIRED`, puis sa collection est supprimée.
5. **Barrière transactionnelle [Proposed]** : `RecordExtraction` lit les index
   `ACTIVE` et `BUILDING` sous `SELECT … FOR SHARE` de la ligne du corpus et
   crée leurs `Indexation` dans la même transaction ; `ActivateSearchIndex`
   prend `SELECT … FOR UPDATE` sur cette ligne et vérifie que toutes les
   versions publiées de documents actifs sont `COMPLETED` dans l'index
   candidat ; `PublishDocumentVersion` lit l'index actif sous `FOR SHARE`. Une
   activation ne peut donc pas s'intercaler entre une extraction et la création
   de ses indexations.
6. **Profil initial : `intfloat/multilingual-e5-base`** — 768 dimensions,
   512 tokens maximum, préfixes `query: ` et `passage: ` obligatoires, distance
   cosine, vecteurs normalisés. **Ce choix est révisable.** Le profil
   définitif sera fixé par un **nouvel ADR** fondé sur le benchmark de S9
   (`multilingual-e5-small`, `multilingual-e5-base`, `bge-m3`, éventuellement
   `Qwen3-Embedding-0.6B`, sur le jeu d'évaluation de S5). Cet ADR ne sera pas
   modifié pour enregistrer ce choix.
7. **Rechunking** : un profil dont la longueur maximale diffère peut imposer
   une nouvelle `ChunkingPolicy`, donc un nouveau lot de chunks pour ce seul
   index. Les citations restent stables (version, section, pages).

## Alternatives envisagées

- **Alias Qdrant comme source de l'index actif** : bascule atomique côté Qdrant,
  mais l'état actif doit rester arbitré par PostgreSQL (ADR 0010).
- **Profil unique non versionné** : impossible de migrer sans interruption ni
  mélange.
- **Choisir dès maintenant `bge-m3`** : meilleur contexte (8192 tokens), mais
  environ deux fois la RAM d'`e5-base`, chargée à la fois dans l'API (requêtes)
  et dans le worker (passages) ; à justifier par mesure.
- **`multilingual-e5-small`** : plus léger, qualité moindre ; candidat du
  benchmark.

## Conséquences

- La recherche lit toujours l'index `ACTIVE` dans PostgreSQL et charge le
  modèle de son profil.
- Pendant une migration, deux modèles peuvent être chargés et deux collections
  coexistent.
- L'activation dépend de la couverture complète : un échec d'indexation dans
  l'index candidat bloque la bascule jusqu'à correction.

## Risques et compromis

- RAM et durée du backfill lors d'une migration.
- Préfixes E5 oubliés ou inversés : dégradation silencieuse, couverte par des
  tests unitaires de l'adapter.
- Verrou du corpus : contention négligeable pour un usage mono-utilisateur, à
  réévaluer en cas de forte concurrence d'imports.

## Conditions de réévaluation

- Résultats du benchmark S9 (nouvel ADR obligatoire).
- Besoin de plusieurs corpus (le périmètre d'unicité de l'index actif devient
  le corpus choisi).
- Recherche hybride dense + sparse.

## Liens

- ADR [0009](0009-pedagogical-chunking-knowledge-units.md),
  [0010](0010-postgresql-source-of-truth-qdrant-derived-index.md),
  [0008](0008-custom-pipeline-docling-extraction.md).
- Conception : [knowledge-base.md §13](../conception/bounded-contexts/knowledge-base.md#13-changement-de-modèle-dembeddings),
  [feuille de route S9](../conception/bounded-contexts/knowledge-base-roadmap.md#s9--benchmark-des-embeddings-et-migration-bluegreen).

## Suivi (2026-11-18)

Faits de l'implémentation de S4, le corps de l'ADR est inchangé.

- **Profil initial** (`MULTILINGUAL_E5_BASE`,
  `backend/src/knowledge_base/domain/value_objects/embedding_profile.py`) :
  `intfloat/multilingual-e5-base`, révision épinglée
  `d128750597153bb5987e10b1c3493a34e5a4502a` (commit relevé sur le Hub le
  2026-11-18), 768 dimensions, cosine, vecteurs normalisés, préfixes
  `query: ` / `passage: `, 512 tokens, tokenizer `xlmr-d1287505` (XLM-R livré
  avec le modèle à cette révision).
- **Politique de production** : `kb-chunking-v1` comptée avec ce tokenizer,
  identité `kb-chunking-v1+xlmr-d1287505`, budget 508 tokens. Sur les fixtures
  du corpus (12 chunks), le plus long chunk encodé avec préfixe et tokens
  spéciaux fait 375 tokens : le comptage du `TokenCounter` (sans préfixe ni
  tokens spéciaux) plus 4 est exact.
- **Premier index** : créé et activé sur corpus vide par `python -m
  runtime.provision` (service Compose `kb_provision`), idempotent ; collection
  Qdrant `kb_index_<index_id>` créée avant la transaction.
- **Barrière** (point 5) : en place pour `RecordExtraction` (`FOR SHARE`) et
  `PublishDocumentVersion` (`FOR SHARE`) ; `ActivateSearchIndex` sous
  `FOR UPDATE` arrive avec S9. Le point reste Proposed jusqu'à S9.
- **Correction (revue S4, 2026-11-18)** de la formulation précédente, qui
  écrivait « l'agrégat porte déjà I10 et le contrôle de couverture » :
  l'agrégat porte I10 (au plus un index `ACTIVE`, l'ancien devient `RETIRED`),
  mais **pas la preuve de couverture d'I11**. `KnowledgeCorpus.activate_index`
  reçoit un booléen `coverage_complete` qu'il ne fait que vérifier ; le seul
  appelant, le provisionnement initial, le passe à `True` en dur
  (`provision_search_index_handler.py`), ce qui n'est juste que parce que le
  corpus n'a encore aucun index, donc aucune version publiée (I9). Le calcul
  réel de la couverture (toutes les versions publiées de documents `ACTIVE`
  `COMPLETED` dans l'index candidat, sous `FOR UPDATE` du corpus) arrive avec
  `ActivateSearchIndex` en S9.
- **Clé étrangère de l'index actif** (revue S4) : `knowledge_corpus
  (corpus_id, active_index_id)` référence `search_indexes(corpus_id,
  index_id)`, `DEFERRABLE INITIALLY DEFERRED` (migration `c43c7e850745`) :
  la base garantit que l'index actif existe et appartient au corpus ; I10
  reste porté par l'agrégat et l'index unique partiel.
- **Mesures** (CPU, `SentenceTransformer` en `device="cpu"`, lot de 32 chunks
  de 152 tokens en moyenne) :

  | | Conteneur Linux arm64 (image `production`, 18 vCPU) | Hôte macOS arm64 |
  |---|---|---|
  | Chargement, cache chaud, import de torch compris | 3,65 s | 5,02 s |
  | RSS après chargement | 904 Mio | 1 023 Mio (pic) |
  | RSS après un lot de 32 | 1 667 Mio | 1 863 Mio (pic) |
  | Encodage, médiane de 5 lots | 91 ms/chunk (2,92 s le lot) | 51 ms/chunk (1,62 s) |
  | Deux encodages du même texte | écart maximal 0 | écart maximal 0 |

  La RAM du worker d'ingestion avoisine donc 1,7 Gio dès la première
  indexation ; l'API ne charge pas le modèle en S4 (recherche : S5). Le
  benchmark de S9 comparera ces chiffres à ceux des autres candidats.

S5 (2026-11-18) : l'API charge le modèle du profil initial à la **première
recherche** (pas au démarrage) ; RSS du processus serveur 146 → 1 267 Mio,
premier appel 4,3 s, encodage d'une requête 63 ms médians dans le conteneur
de dev (13 ms sur l'hôte macOS). Garde-fou en attendant S9 : la recherche
refuse (`SEARCH_PROFILE_MISMATCH`) un index actif dont le profil diffère de
celui de l'embedder de l'API, au lieu de comparer des vecteurs incompatibles.
Le chargement « du modèle de l'index actif » (Conséquences) arrive avec la
migration blue/green.

## S9 — migration blue/green (2026-11-18)

Le corps de l'ADR est inchangé. Faits de l'implémentation de S9
([knowledge-base.md §13, précisions S9](../conception/bounded-contexts/knowledge-base.md#13-changement-de-modèle-dembeddings),
[README du contexte](../../backend/src/knowledge_base/README.md#s9--benchmark-des-embeddings-et-migration-bluegreen)) :

- **Profil (point 6)** : le profil définitif est proposé par l'**ADR 0013**
  ([Proposed](0013-definitive-embedding-profile.md)), qui complète et, pour le
  seul choix du profil, remplace le point 6 une fois accepté ; il propose de
  **conserver `multilingual-e5-base`** (budget mémoire mesuré ; Qwen3-Embedding-0.6B,
  meilleur en qualité, en chemin de montée en gamme). Le présent ADR n'est pas
  modifié. Mesures : [rapport de benchmark](../conception/bounded-contexts/knowledge-base-embedding-benchmark.md).
- **Migration (point 4)** : `ProvisionSearchIndex` reste la commande de
  démarrage de la stack (premier index, idempotente) ; le démarrage d'une
  migration est la commande distincte `StartIndexMigration` (index
  `BUILDING`, sa collection, backfill des versions publiées et extraites de
  documents actifs ; un seul index `BUILDING` à la fois). Pilotage par
  `python -m runtime.index_migration start <cible> | activate <index_id>`,
  sans route HTTP ; cibles : catalogue `embedding_targets.CATALOGUE`.
- **Barrière (point 5)** : implémentée et vérifiée sur PostgreSQL réel, sans
  délai (`test_s9_concurrency`) : `StartIndexMigration` et
  `ActivateSearchIndex` tiennent `FOR UPDATE` dès leur première lecture ;
  une extraction en cours retarde le provisionnement, dont le backfill voit sa
  version ; une publication en cours retarde l'activation, dont la preuve la
  voit et refuse. La couverture d'I11 est **calculée** dans la transaction
  de l'activation. S9 ajoute une garde : activation refusée tant qu'une
  synchronisation de visibilité d'un document publié est en attente (points
  du candidat encore invisibles). Le statut de ce point reste celui de
  l'index des ADR (Proposed) jusqu'à sa validation par l'utilisateur.
- **Rechunking (point 7)** : `rechunk_version` produit, pour un index d'une
  autre politique, un second lot de la version (re-parsing de l'original,
  O4), puis son indexation ; une extraction pendant la construction planifie
  ce rechunking au lieu d'ignorer l'index (S4).
- **Conséquences** : la recherche choisit le modèle du profil de l'index
  actif (`EmbedderProvider`, un modèle paresseux par profil du catalogue) ;
  un profil non chargeable reste `SEARCH_PROFILE_MISMATCH` (503), jamais un
  autre modèle. Le worker encode chaque index avec le modèle de son profil
  (un profil non chargeable : `COLLECTION_MISMATCH` pour cet index seul,
  report S4 M3 levé). L'ancien index passe `RETIRED` ; sa collection est
  supprimée par `drop_index_collection` après `KB_INDEX_DROP_DELAY_SECONDS`.
- **Mémoire pendant la transition** (démonstration sur la stack de dev,
  2026-11-18/19) : processus serveur de l'API 1 277 → 1 666 Mio avec deux
  modèles après la bascule ; enfant de `worker_ingestion` 1 964 Mio pendant le
  backfill vers e5-small (e5-base de l'extraction + e5-small) ; 6 190 et 6 467
  recherches pendant les deux migrations, 0 erreur ; ancienne collection
  supprimée 301,7 et 301,4 s après l'activation.
