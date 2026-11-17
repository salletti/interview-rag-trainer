# Knowledge Base — Conception du Bounded Context

> **Statut : Proposed — revue de la proposition du 2026-11-17.**
>
> Ce document décrit un modèle cible à valider par ADR. Il ne signifie ni que le code est implémenté ni que les choix proposés sont déjà Accepted.
>
> **Sources internes :** [001](../001-bounded-contexts.md), [002](../002-event-flows.md), [004](../004-integration-contracts.md), [005](../005-project-structure.md), ADR 0001/0002 (Accepted), ADR 0003/0004/0006 (statut à vérifier avant implémentation).
>
> **Note :** synthèse critique de l'audit Knowledge Base fourni par Claude ; les versions de packages, métriques de ressources et licences mentionnées dans cet audit restent à revalider au moment du lock.

## 1. Mission et limites

**Knowledge Base** gère les documents pédagogiques, leurs versions, l'extraction, les unités de connaissance, l'indexation et la recherche sourcée. Son modèle n'a aucune dépendance à Interview Training ou Assessment.

- **In scope :** import, original immutable blob, dédoublonnage, extraction, unités/chunks, versioning, retraits, indexation, recherche, provenance, récupérabilité.
- **Out of scope :** organisation d'entretiens, correction, calcul de score, décision de la question suivante, suivi des compétences, génération LLM des questions.
- **Relations :** Knowledge Base est upstream pour la recherche ; Interview Training / Assessment consomment un contrat public `KnowledgeSearch` (Open Host Service candidat). Les consommateurs figent les extraits et citations effectivement utilisés dans leurs propres snapshots.
- Aucun autre BC ne lit directement les tables internes Knowledge Base ou Qdrant.

## 2. Langage ubiquitaire

| Terme | Définition |
|---|---|
| `KnowledgeDocument` | Document logique pédagogique, stable entre versions |
| `KnowledgeSource` | Terme existant à réconcilier : provenance/source documentaire, non un second agrégat concurrent |
| `DocumentVersion` | Contenu importé immuable, identifié par empreinte et numéro de version |
| `DocumentSource` | Nom d'origine, média, taille, empreinte SHA-256, date |
| `DocumentKind` | `COURSE`, `QA_SHEET`, `REVISION_SHEET`, `TECHNICAL` |
| `StructuredDocument` | Représentation interne neutre des blocs extraits et de leur provenance |
| `KnowledgeUnit` | Unité pédagogique : Q + R + code + explication, ou section de cours |
| `DocumentChunk` | Fragment indexable d'une unité, avec en-tête et provenance |
| `Citation` | Document/version/section/pages référencés de manière stable |
| `EmbeddingProfile` | Modèle + révision + dimension + normalisation + conventions query/passage + tokenizer |
| `ChunkingPolicy` | Version des règles de découpage, budget tokens, stratégie pour code et tableaux |
| `Indexation` | Travail d'indexation d'une version de document dans un index cible |
| `SearchIndex` | Génération d'index de recherche (BUILDING, ACTIVE, RETIRED) |
| `RetrievedPassage` | Résultat public : texte pertinent, unité et citation vérifiable |
| `Withdrawal` | Retrait logique de la visibilité, suivi d'une purge physique asynchrone |

## 3. Modèle tactique proposé

### 3.1 `KnowledgeDocument` — Aggregate Root

Identité, `title`, `kind`, `topics`, `language`, `status`, `published_version_id`, version optimiste.

- `DocumentVersion` : entité enfant (identifiant, numéro, source/hash, statut, erreur). Son contenu original n'est jamais remplacé.
- États document : `ACTIVE`, `WITHDRAWN`.
- États version candidats : `RECEIVED`, `EXTRACTING`, `EXTRACTED`, `PUBLISHED`, `SUPERSEDED`, `FAILED`.
- Une seule version est publiée par document (unicité garantie en SQL ou via `published_version_id` sous verrou).
- Un nouvel import peut supplanter une version non publiée, mais doit **invalider et empêcher** les anciens workers de publier, via fencing et vérification transactionnelle.

**Réserve DDD :** le lease d'extraction, son heartbeat et son compteur de tentatives sont avant tout un état de processus. Ne pas obliger le rechargement du `KnowledgeDocument` à chaque heartbeat. On peut commencer avec une table de travail séparée et ne promouvoir un `ExtractionJob` en agrégat que si de réels invariants métier le justifient.

### 3.2 `Indexation` — Aggregate Root candidat

Identité logique unique `(document_version_id, search_index_id)`. États `PENDING`, `RUNNING`, `COMPLETED`, `FAILED`. Porte tentative, lease, fencing token, état de reprise et manifest / nombre attendu de chunks.

Cette séparation permet d'indexer la même version dans l'index actif et un nouvel index pendant une migration, sans verrouiller le document pour chaque tentative.

### 3.3 `SearchIndex` — Aggregate Root candidat

Identité, profil d'embeddings, politique de chunking, état `BUILDING/ACTIVE/RETIRED`, version/configuration d'index.

- Au plus un index `ACTIVE` pour un périmètre de recherche défini. La portée de cette unicité (globale ou par tenant/corpus) reste **Open**.
- L'activation est conditionnée à la couverture *complète* du corpus éligible et à un point de synchronisation : importer pendant `BUILDING` ne doit pas créer de trous.
- Les noms physiques de collections et IDs Qdrant sont des détails d'adapter, sauf preuve qu'une identité logique d'index en dépend.

### 3.4 Chunks et unités

- Les `KnowledgeUnit` et `DocumentChunk` sont des données immuables versionnées, persistées hors de l'agrégat document, pour éviter un agrégat contenant des centaines de chunks.
- PostgreSQL conserve texte, unités, relations, hash et citations ; Qdrant ne contient que les vecteurs et le payload de filtrage/reconstruction.
- Une écriture de lot de chunks et la transition `EXTRACTED` doivent être cohérentes (transaction ou mécanisme de staging/finalisation pour les gros imports). Ne pas supposer qu'une transaction unique reste souhaitable pour des millions de chunks.
- La version de `ChunkingPolicy` fait partie de l'identité des artefacts dérivés ; une indexation avec un tokenizer plus court peut exiger un nouveau découpage.

## 4. Pipeline : composants et ports

| Phase | Proposition | Frontière |
|---|---|---|
| Upload | HTTP multipart, validation taille/type, SHA-256, idempotency key | Controller REST |
| Blob original | Volume partagé api/worker adressé par hash, adaptateur remplaçable | `DocumentBlobStore` |
| Parse | Docling via extras ciblés, OCR désactivé en V1 ; TXT natif si nécessaire | `DocumentParser` |
| Segmentation | `KnowledgeUnitSegmenter`, règles par `DocumentKind` | Service Python pur |
| Découpage | `ChunkPlanner`, limites du modèle et provenance | Service Python pur |
| Embeddings | SentenceTransformers ; `multilingual-e5-base` candidat initial | `Embedder` |
| Index | Qdrant Client, collection par génération compatible | `VectorIndex` |
| Recherche | `SearchKnowledge` + contrat `KnowledgeSearch` | Use case + façade publique |
| Traitement | Celery/RabbitMQ, queue dédiée et scheduler/recovery | Controller de tâches et infrastructure |

**Pas de LangChain ni de LlamaIndex en V1 par défaut.** Cette décision reste Proposed ; documenter une raison concrète si une abstraction supplémentaire devient nécessaire.

### 4.1 Chunking pédagogique « small-to-big »

1. Docling est adapté vers `StructuredDocument` (titres, paragraphes, code, tableaux, provenance), sans exposer `DoclingDocument` au domaine.
2. Les règles de segmentation construisent des unités : `QA_SHEET` = question + réponse + explications + code ; `COURSE/TECHNICAL` = sections ; `REVISION_SHEET` = définition/fiche.
3. Détecter les Q/R par structure et conventions explicites (`kind: qa_sheet`) avant de se reposer sur des heuristiques fragiles.
4. Unité courte : un chunk ; unité longue : fragments sur frontières de blocs avec rappel contextuel. Un code/tableau surdimensionné exige une stratégie de fallback **documentée**, pas la promesse absolue « jamais coupé ».
5. Embedding d'un fragment avec chemin de section et question racine ; retour de l'unité complète ou d'une fenêtre bornée sous budget de contexte.
6. Chaque résultat conserve une citation stable vers la version et la section originales.

**Mesures à réaliser :** comparaison avec overlap contrôlé ; absence d'overlap n'est pas un invariant DDD. Une unité longue ne doit pas faire exploser le contexte LLM lors du small-to-big.

### 4.2 EmbeddingProfile et compatibilité

- Préfixes `query: ` et `passage: ` obligatoires pour la famille E5 ; modèle et révision du tokenizer fixés.
- Vecteurs normalisés si requis ; distance cosine compatible avec collection.
- Profil candidat `intfloat/multilingual-e5-base` : pas encore choix définitif.
- Comparer `multilingual-e5-small`, `e5-base`, `bge-m3` et éventuellement `Qwen3-Embedding-0.6B` avec le corpus FR/EN ; mesurer qualité, RAM, CPU et latence.
- Ne jamais mélanger dans une collection des embeddings de profils incompatibles ; bascule de génération blue/green.

## 5. Commandes et événements

### Commandes métier candidates

- `ImportDocument`, `ImportDocumentVersion`
- `WithdrawDocument`, `RetryIngestion`
- `PublishDocumentVersion`
- `ProvisionSearchIndex`, `ActivateSearchIndex`
- `SearchKnowledge` est une **Query**, pas une commande.

### Commandes de processus / worker

- `ClaimExtraction`, `RecordExtraction`, `FailExtractionAttempt`
- `ClaimIndexation`, `CompleteIndexation`, `FailIndexationAttempt`
- `PurgeDocument`, `ReconcileIngestion`

### Domain Events locaux candidats

`DocumentImported`, `DocumentVersionExtracted`, `IndexationCompleted`, `DocumentVersionPublished`, `DocumentWithdrawn`, `SearchIndexActivated`.

### Integration Events

Réserver `knowledge.document_version_published.v1` et `knowledge.document_withdrawn.v1` pour le moment où un consommateur réel apparaît. Ne pas confondre événement métier publié et état interne « ingestion démarrée ».

## 6. Transactions, cohérence et reprise

| Étape | Transaction PostgreSQL | Travail hors transaction |
|---|---|---|
| Import | Créer document/version, déduplication + work item durable | Stream fichier temporaire, hash et stockage blob idempotent |
| Extraction | Claim avec lease/fencing ; finalisation cohérente des chunks et état `EXTRACTED` | Parse, segmentation, chunking |
| Indexation | Claim, puis finalisation sous fencing | Embeddings par lots, upserts Qdrant |
| Publication | Bascule SQL de version visible et planification de purge | Purge de la version supersédée |
| Retrait | `WITHDRAWN` immédiatement en SQL | Purge Qdrant/blobs ultérieure |

- `UNIQUE(document_id, content_hash)`, idempotency key/fingerprint et identifiants de points déterministes.
- Worker d'ingestion dédié, concurrence 1 pour limiter la RAM initialement ; scheduler, récupérations périodiques et work queue durables.
- Leases expirés → reprise sans publier un résultat d'une tentative périmée.
- L'état SQL fait foi, mais **la cohérence avec Qdrant n'est pas atomique** : prévoir contrôles, filtres et reconciliation.
- Un simple `count` Qdrant égal au nombre attendu ne garantit pas intégrité/identité/bonne version : comparer le manifest d'IDs et vérifier la réussite des upserts, puis réconcilier.
- Une activation blue/green nécessite une barrière de synchronisation tenant compte des imports survenus pendant la construction de l'index.
- Retrait immédiat : chaque recherche doit vérifier la visibilité actuelle en SQL. Même avec cette règle, il faut préciser la sémantique si un retrait est concurrent d'une lecture déjà engagée.
- Le suréchantillonnage Qdrant puis filtrage SQL des versions visibles peut réduire le Recall : tester et envisager un filtre d'éligibilité dans Qdrant, un backfill itératif ou une autre stratégie avant de garantir un top-k complet.
- Une suppression concurrente à un upsert nécessite fencing / contrôles de statut avant publication et un balayage de purge.

**Politique d'erreurs proposée :** erreurs déterministes → échec terminal, erreurs transitoires → retry borné + backoff, crash/OOM → lease expiré et récupération. Ne pas écarter une DLQ RabbitMQ par principe ; son utilité dépendra du comportement du relais/consumer et de l'observabilité.

## 7. Contrat public de recherche

```python
class KnowledgeSearch(Protocol):
    async def search(self, query: KnowledgeQuery) -> list[RetrievedPassage]: ...
```

`KnowledgeQuery` : texte, langue?, topics?, kinds?, document_ids?, limite, contraintes d'accès si nécessaire.

`RetrievedPassage` : unit_id, texte, score, version, source et citation (document, titre, chemin de section, pages), index_id.

Les consommateurs recopient les extraits et citations nécessaires dans leurs snapshots ; les références restent stables malgré les réindexations. Le contrat public ne contient ni objet Qdrant ni ORM.

## 8. Structure Clean Architecture cible

Conforme au [005](../005-project-structure.md), **créer les dossiers seulement lorsqu'une slice les utilise**.

```text
backend/src/knowledge_base/
  __init__.py
  README.md
  domain/
    entities/                 # knowledge_document, indexation, search_index
    value_objects/            # document_source, citation, embedding_profile...
    services/                 # knowledge_unit_segmenter, chunk_planner
    events/
    exceptions/
    providers/                # repositories, parser, embedder, vector index, blob store, UoW
  use_cases/
    import_document/
    import_document_version/
    claim_extraction/
    record_extraction/
    claim_indexation/
    complete_indexation/
    publish_document_version/
    withdraw_document/
    purge_document/
    reconcile_ingestion/
    search_knowledge/
  adapters/
    controller/
      rest/                   # FastAPI
      tasks/                  # Celery → use cases
      contracts/              # adaptateur du contrat public KnowledgeSearch
    gateway/
      persistence/            # SQLAlchemy, work queue
      parsing/                # Docling
      embedding/              # SentenceTransformers
      vector_store/           # Qdrant
      blob/                   # filesystem
  contracts/
    providers/                # KnowledgeSearch Protocol
    result/                   # KnowledgeQuery / RetrievedPassage / Citation DTO
  frameworks/
    sqlalchemy/               # tables et Metadata, uniquement si besoin
```

Ne pas placer Qdrant, Docling ou SentenceTransformers dans `domain/`. Les détails des frameworks sont dans leurs adapters. Les interfaces sont définies en fonction des besoins, non pour remplir l'arborescence.

## 9. Vertical slices et tests

| Slice | Objectif | Critère vérifiable |
|---|---|---|
| **S0** | Préparer packages/migrations, stockage partagé, worker dédié, work queue/recovery minimal, tests archi | CI + intégration réelles ; pas de travail perdu à l'enqueue |
| **S1** | Modèle KnowledgeDocument / Indexation / SearchIndex | Invariants testés sans I/O |
| **S2** | Import Markdown/TXT → PostgreSQL + blob | Idempotency-Key et doublons testés |
| **S3** | Parser Markdown, unités et chunking | Golden tests Q/R/code/provenance |
| **S4** | Embeddings + Qdrant + indexation récupérable | Rejeu après crash sans doublon ; identité/manifest vérifiés |
| **S5** | SearchKnowledge, filtrage/version et évaluation | Recall@5, MRR, intégrité Q/R/code FR/EN |
| **S6** | PDF/DOCX Docling | Fixtures réelles, mesure CPU/RAM, PDF OCR_REQUIRED |
| **S7** | Versions, activation/publication, retrait/purge | Aucune version obsolète visible ; concurrence testée |
| **S8** | Durcissement des reprises/réconciliation | Retries, échecs terminalisés, orphelins |
| **S9** | Benchmark et migrations de profils | Comparaisons qualité/coût et bascule d'index testée |

**Ajustement de séquencement :** les protections indispensables (leases, recovery, retries basiques) doivent exister dès S2–S4 ; S8 complète la robustesse mais ne doit pas être le premier moment où l'on peut récupérer une tâche perdue.

## 10. Décisions à enregistrer après validation

- ADR 0007 : création du BC Knowledge Base et contrats publics.
- ADR 0008 : extracteur Docling et pipeline personnalisé.
- ADR 0009 : chunking pédagogique / small-to-big.
- ADR 0010 : PostgreSQL vérité, Qdrant index dérivé, règles de visibilité.
- ADR 0011 : EmbeddingProfile, SearchIndex et migration blue/green.
- ADR 0012 : stockage des originaux et stratégie de purge.
- Réexaminer ADR 0003/0004/0006 selon leur statut **réel** en dépôt, sans les marquer Accepted automatiquement.

## 11. Points ouverts

1. Profil d'embeddings définitif après benchmark.
2. Politique de doublon inter-documents : refuser, lier ou autoriser plusieurs documents logiques.
3. Unicité de SearchIndex par corpus / scope.
4. Rechunking lors d'un changement de profil et stabilité des citations.
5. API/worker : images distinctes ou groupes de dépendances.
6. Stratégie de retrieval avant filtrage SQL, avec Recall@k mesuré.
7. Intégrité vérifiée des points Qdrant au-delà du compte.
8. Support TXT, formats OCR, limites de fichiers, licences et performances mesurées.
9. Règles de suppression et d'accès à de futurs documents privés.
10. Niveau nécessaire de fiabilité outbox/work queue/DLQ selon les types de tâches.
