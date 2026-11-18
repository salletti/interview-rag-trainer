# Knowledge Base

Bounded Context ([ADR 0007](../../../docs/decisions/0007-knowledge-base-bounded-context.md)) chargé des documents de connaissances, de leur ingestion (extraction, chunking, embeddings) et de la recherche sémantique publiée vers les autres contextes. Conception : [knowledge-base.md](../../../docs/conception/bounded-contexts/knowledge-base.md) ; feuille de route : [S0–S9](../../../docs/conception/bounded-contexts/knowledge-base-roadmap.md).

## État : slices S0 à S3

### S0 — infrastructure de traitement durable

S0 livre uniquement le mécanisme **générique** de travail durable ([S0](../../../docs/conception/bounded-contexts/knowledge-base-s0.md), [ADR 0004, complément work queue](../../../docs/decisions/0004-reliable-messaging.md#complément--work-queue-durable-accepted-2026-11-17)). Aucun agrégat, aucun endpoint, aucun `kind` métier : le registre de handlers est vide en production.

| Garantie | Assurée par |
|---|---|
| Un travail n'existe que si la transaction métier est commitée | `enqueue` dans la session du handler métier (`INSERT … ON CONFLICT (dedup_key) DO NOTHING`) |
| Un travail commité n'est pas perdu si sa notification se perd | tâche `dispatch` (après commit) et `redispatch_after` |
| Plusieurs notifications d'un même item sont sans danger | claim conditionnel (`PENDING` → `RUNNING`) |
| Au plus une finalisation PostgreSQL par item | fencing token vérifié dans la transaction de finalisation |
| Un lease perdu (crash, OOM, `time_limit`) est repris | tâche `recover` |
| Les effets externes peuvent être exécutés plusieurs fois | **non garanti unique** : ils doivent être idempotents (S0 §6) |

Le message RabbitMQ n'est qu'un signal de déclenchement (`acks_late = False`) ; PostgreSQL fait foi.

### S1 — domaine `KnowledgeDocument` (Python pur)

Agrégat `KnowledgeDocument` et entité interne `DocumentVersion` ([knowledge-base.md §3.1, §4.1, §5.1–5.2](../../../docs/conception/bounded-contexts/knowledge-base.md#31-knowledgedocument--aggregate-root-accepted)). En S1 : aucune persistance, aucun use case, aucun endpoint ; depuis S2, l'agrégat est persisté et appelé par les imports (ci-dessous). `domain/{entities,value_objects,events,exceptions}` n'importe que la bibliothèque standard et le domaine du contexte (règle `knowledge_base_pure_domain_is_stdlib_only`).

- **Construction** : `KnowledgeDocument.import_new(...)` (document `ACTIVE`, version 1 `RECEIVED`) ; `KnowledgeDocument.restore(...)` réhydrate un état persisté tel quel, sans événement ni contrôle de transition (chemin de la persistance, S2). Le domaine ne génère ni identifiant ni date : les UUID sont fournis par l'appelant, `created_at = source.imported_at`.
- **Opérations** : `import_version`, `record_extraction`, `fail_extraction`, `retry_extraction`, `publish`, `withdraw`. Une opération refusée lève une `KnowledgeDocumentError` (`domain/exceptions/document_errors.py`) **sans aucun effet** (ni état, ni événement).
- **Transitions de version** : une seule table (`_TRANSITIONS`, diagramme §5.2) ; `DISCARDED` et `SUPERSEDED` sont terminaux. `failure` n'est renseigné qu'à l'état `FAILED` (toute autre transition l'efface).
- **`version`** (verrou optimiste) vaut 0 à la création et n'est incrémenté que par la persistance.

| Invariant (§4.1) | Mécanisme dans l'agrégat | Refus |
|---|---|---|
| I1 — version immuable | `DocumentVersion` dataclass figée, remplacée par `dataclasses.replace` ; aucun setter | — |
| I2 — numéros strictement croissants | numéro = max existant + 1 (jamais réutilisé, y compris après `restore`) | — |
| I3 — une empreinte par document | comparaison à **toutes** les versions, `DISCARDED` comprises | `DuplicateContentError` |
| I4 — au plus une `PUBLISHED` | la publication fait passer la précédente `SUPERSEDED` | — |
| I5 — publication monotone | `number > numéro publié`, après les contrôles « document `ACTIVE` » et « version `EXTRACTED` » | `DocumentWithdrawnError`, `VersionNotFoundError`, `InvalidVersionTransitionError`, `StalePublicationError` |
| I6 — N+1 écarte les non publiées | à l'import, toute version antérieure `RECEIVED`, `EXTRACTED` ou `FAILED` passe `DISCARDED` | — |

La partie SQL de ces invariants (contraintes, verrou optimiste, index unique partiel) et l'annulation des `work_items` d'I6 sont livrées en S2 (ci-dessous). Retrait : `WITHDRAWN`, `visibility_revision + 1`, versions inchangées ; un second retrait et tout import ultérieur lèvent `DocumentWithdrawnError`. `visibility_revision` n'augmente qu'à la publication et au retrait.

Domain Events (`domain/events/document_events.py`, dataclasses figées, base `DocumentEvent(document_id)`), relus par `pull_events()` qui rend la liste puis la vide :

| Événement | Émis par | Champs |
|---|---|---|
| `DocumentImported` | `import_new`, `import_version` | `version_id`, `number`, `content_hash` |
| `DocumentVersionDiscarded` | `import_version`, un par version écartée, **après** `DocumentImported`, par numéro croissant | `version_id`, `number`, `replaced_by_version_id` |
| `DocumentVersionExtracted` | `record_extraction` | `version_id` |
| `DocumentVersionPublished` | `publish` | `version_id`, `number`, `visibility_revision`, `superseded_version_id` (ou `None`) |
| `DocumentWithdrawn` | `withdraw` | `visibility_revision` |

`fail_extraction` et `retry_extraction` n'émettent pas d'événement en S1.

### S2 — import Markdown/TXT, stockage de l'original, dédoublonnage

Cas d'usage (`use_cases/`, un dossier par commande ; étapes communes dans `use_cases/import_steps.py`) :

| Commande | Handler | Effet |
|---|---|---|
| `ImportDocument(title, kind, topics, language, filename, media_type, content, idempotency_key=None)` | `ImportDocumentHandler` | nouveau document `ACTIVE`, version 1 `RECEIVED` ; contenu déjà connu → document existant (`created=False`) |
| `ImportDocumentVersion(document_id, filename, media_type, content, idempotency_key=None)` | `ImportDocumentVersionHandler` | version N+1 `RECEIVED` ; versions antérieures non publiées `DISCARDED` (I6) et leur item `extract_version` annulé |

Les deux rendent `ImportDocumentResult(document_id, version_id, number, created)`. Déroulé :

1. contrôle du type **avant toute écriture** : liste blanche type de média + extension (`text/markdown` : `.md`, `.markdown` ; `text/plain` : `.txt`), paramètres (`; charset=…`) retirés du type stocké ;
2. dépôt du blob **hors transaction** (`DocumentBlobStore.put`) : SHA-256 au fil du flux, limite `KB_MAX_UPLOAD_BYTES` appliquée pendant la lecture ; un fichier vide est accepté (il échouera à l'extraction, S3) ;
3. **une** transaction (`UnitOfWork`) : rejeu éventuel de la clé d'idempotence, document / version, item `extract_version` (`dedup_key = extract_version:<version_id>`, payload `document_id`, `version_id`), annulations d'I6, clé d'idempotence, `commit`.

Un échec de la transaction laisse un blob orphelin (purgé par la réconciliation S7/S8), jamais une ligne sans blob. L'item `extract_version` est traité depuis S3 (ci-dessous).

- **Dédoublonnage et O2** ([knowledge-base.md §16](../../../docs/conception/bounded-contexts/knowledge-base.md#16-points-ouverts)) : `UNIQUE(content_hash)` global. Même contenu sans cible → document existant, quelles que soient les métadonnées ; version ciblée dont le contenu appartient à un autre document → `DuplicateContentOtherDocumentError` ; même document → `DuplicateContentError` (I3). Le contenu d'un document retiré ne peut pas être réimporté (`DocumentWithdrawnError`).
- **Idempotence** (002) : `Idempotency-Key` facultatif. Empreinte de requête = SHA-256 d'un JSON canonique (opération, document cible, titre, nature, thèmes, langue, nom de fichier, type de média, empreinte du contenu). Même clé et même empreinte → résultat enregistré, sans ré-exécution (même `created`) ; empreinte différente → `IdempotencyKeyConflictError`. La clé est écrite dans la transaction de l'import. Les clés n'expirent pas encore.
- **Concurrence** : deux versions concurrentes d'un même document → verrou optimiste (`documents.version`), le perdant reçoit `ConcurrentModificationError`, sans réessai automatique. Deux imports simultanés du même nouveau contenu, ou de la même clé → l'adapter traduit la violation d'unicité en `ContentHashTakenError` / `IdempotencyKeyTakenError` (aucune `IntegrityError` ne sort de la persistance) ; le handler rejoue **une fois** sa transaction, qui voit alors le gagnant.

Routes (`adapters/controller/rest/`, montées par `register`) :

| Méthode et chemin | Corps (multipart) | Succès |
|---|---|---|
| `POST /api/v1/knowledge-base/documents` | `file`, `title`, `kind` (`COURSE`, `QA_SHEET`, `REVISION_SHEET`, `TECHNICAL`), `language`, `topics` (répétable, facultatif) | 202 `{document_id, version_id, number}` ; 200, même corps, si le contenu était déjà connu |
| `POST /api/v1/knowledge-base/documents/{document_id}/versions` | `file` | 202 `{document_id, version_id, number}` |

En-tête facultatif `Idempotency-Key` (1 à 255 caractères). Erreurs `{code, message, ...}` :

| HTTP | `code` | Champs complémentaires | Cause |
|---|---|---|---|
| 413 | `PAYLOAD_TOO_LARGE` | `max_bytes` | fichier au-delà de `KB_MAX_UPLOAD_BYTES` ; ni blob ni ligne |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | `filename`, `media_type` | couple type / extension hors liste blanche |
| 404 | `DOCUMENT_NOT_FOUND` | `document_id` | document cible inconnu |
| 409 | `DUPLICATE_CONTENT` | `document_id`, `version_id` (version existante) | contenu déjà présent dans ce document (I3) ou dans un autre (O2) |
| 409 | `IDEMPOTENCY_KEY_CONFLICT` | — | même clé, autre requête |
| 409 | `DOCUMENT_WITHDRAWN` | `document_id` | document retiré (nouvelle version ou réimport de son contenu) |
| 409 | `CONCURRENT_MODIFICATION` | — | verrou optimiste perdu (ou course perdue deux fois de suite) ; le client rejoue |
| 422 | format FastAPI (`detail`) | — | formulaire invalide (champ manquant, `kind` inconnu, identifiant non UUID) |

Le fichier n'est jamais lu en entier en mémoire : Starlette stocke la partie multipart dans un fichier temporaire (en mémoire jusqu'à 1 Mio, sur disque au-delà) **avant** l'appel de la route, puis le routeur la transmet au blob store par morceaux de 64 Kio. Pic mesuré (`tracemalloc`) pour un dépôt de 20 Mio : environ 120 Kio. La limite ne s'applique donc qu'après la réception complète par Starlette ; borner le corps HTTP lui-même relève du serveur ou du proxy.

Stockage (`adapters/gateway/blob/filesystem_blob_store.py`, ADR 0012) : flux écrit sous `KB_BLOB_ROOT/tmp/`, `fsync`, puis `os.replace` vers `KB_BLOB_ROOT/sha256/<aa>/<bb>/<empreinte>` et `fsync` du répertoire ; aucun fichier n'apparaît au chemin final avant la fin du flux ; écriture idempotente, y compris pour des dépôts concurrents du même contenu ; I/O disque via `asyncio.to_thread`.

Tables (migration `65b08637988e`, schéma `knowledge_base`) :

| Table | Contenu | Contraintes |
|---|---|---|
| `documents` | métadonnées de l'agrégat, `topics text[]`, `status`, `published_version_id`, `visibility_revision`, `version` (verrou optimiste) | `CHECK` sur `status` et `kind` ; pas de clé étrangère vers les versions (référence circulaire) |
| `document_versions` | `number`, `status`, `filename`, `media_type`, `size_bytes`, `content_hash`, `imported_at`, `failure_*` | `UNIQUE(content_hash)` (I3 + O2), `UNIQUE(document_id, number)` (I2), clé étrangère vers `documents`, `CHECK` sur `status` et « `failure_*` renseignés ssi `FAILED` », index unique partiel « une `PUBLISHED` par document » (I4) |
| `idempotency_keys` | `key` (clé primaire), `fingerprint`, résultat (`document_id`, `version_id`, `number`, `created`) | sans clé étrangère : un enregistrement rend un résultat passé |

Persistance (`adapters/gateway/persistence/`) : `SqlAlchemyUnitOfWork(session_factory)` — une session et une transaction par `async with`, partagées par le dépôt du document, `SqlAlchemyWorkQueue` (S0) et le dépôt des clés. `save(document)` écrit le document **avant** ses versions (`UPDATE … WHERE version = <lue>`, zéro ligne → `ConcurrentModificationError`), puis seulement les versions nouvelles ou modifiées par rapport à l'état lu dans la transaction.

### S3 — Docling Markdown, `KnowledgeUnit` et chunking

Une version `RECEIVED` devient `EXTRACTED` avec ses unités et chunks, par le handler du `kind` `extract_version` (file `knowledge_base.ingestion`, `max_attempts` 5), enregistré en production par `build_work_handlers` ([knowledge-base.md §3.4, §6](../../../docs/conception/bounded-contexts/knowledge-base.md#34-unités-et-chunks--données-dérivées-immuables-accepted), [ADR 0008](../../../docs/decisions/0008-custom-pipeline-docling-extraction.md), [ADR 0009](../../../docs/decisions/0009-pedagogical-chunking-knowledge-units.md)).

**Pipeline** (`adapters/controller/tasks/extract_version_handler.py` → `use_cases/record_extraction/`) :

1. `RecordExtractionHandler.prepare`, **hors transaction** : relecture de l'agrégat (Unit of Work brève, sans commit) ; rien à faire si le document est absent ou retiré, ou si la version n'est plus `RECEIVED` ; lecture du blob ; puis **dans un thread** (`asyncio.to_thread`) : `DocumentParser` → `StructuredDocument`, `KnowledgeUnitSegmenter` (selon `document.kind`), `ChunkPlanner`. Pendant ce calcul, le handler prolonge le lease (`ctx.heartbeat()` toutes les `KB_WORK_HEARTBEAT_INTERVAL_SECONDS`, un tiers du lease par défaut) ; un heartbeat refusé arrête la tentative sans finalisation.
2. Un seul `ctx.finalize` : `apply` relit l'agrégat **dans la transaction de finalisation** ; si la version est toujours `RECEIVED` et le document `ACTIVE` : `record_extraction`, `save` (verrou optimiste), puis le lot `(version_id, chunking_policy_version)` ; sinon rien n'est écrit et l'item se termine `SUCCEEDED` (version supplantée ou document retiré entre-temps : choix S3, ce n'est pas une erreur). Aucune `Indexation` en S3.
3. Échec terminal : le hook `on_terminal_failure` passe la version `RECEIVED` → `FAILED` avec `FailureReason(code, message, retryable)` ; sans effet si la version a changé d'état.

**Parseurs** (`adapters/gateway/parsing/`, port `DocumentParser`, synchrone) : `RoutingDocumentParser` aiguille sur le type de média (sans paramètre ni casse) ; `text/markdown` → `DoclingMarkdownParser`, `text/plain` → `PlainTextParser` (adapter natif, point O7). Docling n'est importé que par `docling_markdown_parser.py` (test d'architecture `test_docling_confinement`), et seulement à la première conversion : l'API compose le handler sans charger Docling.

- Markdown : UTF-8 (BOM toléré) ; **front-matter lu avant Docling** (`---` / `clé: valeur` / `---`) → `metadata`, jamais en blocs ; niveaux de titres restitués (Docling rend `#` en `TitleItem`, `##` en `SectionHeaderItem(level=1)`) ; paragraphe à mise en forme recollé (code en ligne entre accents graves, retours à la ligne → espaces) ; code avec le langage reconnu par Docling en minuscules (`None` si inconnu) ; listes aplaties ; tableau → une ligne `| a | b |` par rangée ; pas de page.
- TXT : une suite de lignes non vides = un bloc ; `CodeBlock` (indentation commune retirée) si toutes ses lignes sont indentées de 4 espaces ou d'une tabulation, `Paragraph` sinon ; ni titre, ni page, ni métadonnée.

**Segmentation** (`domain/services/knowledge_unit_segmenter.py`, heuristiques O13) :

| `DocumentKind` | Unités |
|---|---|
| `QA_SHEET` | une unité `QA` par question : du titre-question jusqu'à la question suivante ou un titre de niveau **strictement inférieur** ; les titres du corps (« Réponse », « Explication ») restent des blocs et n'entrent pas dans le chemin ; contenu hors question → unités `SECTION` ; **aucune question détectée → découpe d'un cours** |
| `COURSE`, `TECHNICAL` | une section avec ses sous-sections si le tout (blocs rendus, hors en-tête) tient dans `max_tokens`, sinon son corps propre puis descente ; le texte avant le premier titre est une unité au chemin vide, qui ne fusionne jamais |
| `REVISION_SHEET` | une unité `DEFINITION` par section la plus haute portant du corps, sous-sections comprises, quel qu'en soit le volume |

- Titre-question (heuristique) : préfixe `Question`/`Q` (casse indifférente, numéro facultatif) suivi de `:`, ou titre terminé par `?` ; `question_text` = le titre sans ce préfixe.
- Convention explicite prioritaire : front-matter `kind: qa_sheet` → **tout titre de niveau 2** est une question, quel que soit le `DocumentKind` déclaré ; les titres « question » plus profonds sont du corps. Seule la valeur `qa_sheet` est reconnue en S3.
- Une unité sans autre bloc que des titres n'est pas produite ; aucun paragraphe ni bloc de code n'est perdu ou dupliqué (vérifié sur tout le corpus).
- `unit_id` = UUIDv5(version, politique, rang) ; `page_span` = pages min/max des blocs.

**Chunking** (`domain/services/chunk_planner.py`) : `contextual_text` = en-tête (titre du document ; chemin `A > B` s'il n'est pas vide ; `Question : …` pour une unité `QA`) + ligne vide + blocs rendus. Le `token_count` du texte **complet** ne dépasse jamais `max_tokens`. Remplissage séquentiel par blocs entiers ; un bloc qui ne tient pas seul est découpé, chaque fragment est un chunk et tout fragment sauf le premier commence par le marqueur `(suite)` sur sa propre ligne (la place du marqueur est réservée pour chaque fragment).

| Bloc trop grand | Frontières successives |
|---|---|
| code | `policy.fallbacks` : lignes vides, puis définitions `def`/`async def`/`class` en colonne 0 (décorateurs `@…` qui les précèdent compris), puis lignes |
| tableau | rangées |
| liste | éléments |
| paragraphe | phrases (`.`, `!`, `?`, `…`) |
| titre | — |

Puis, pour tous, les mots, et en dernier recours les caractères : le budget est garanti quel que soit le contenu. **Choix S3 pour les cas non figés par les tests de l'auteur** : paragraphe ou liste seuls trop grands → phrases / éléments puis mots (recollés par une espace : les retours à la ligne internes d'un paragraphe découpé sont perdus) ; **en-tête de plus de la moitié du budget → tronqué aux mots et suivi de ` …`** (la moitié du budget reste au contenu) ; `overlap_tokens > 0` → `ValueError` (mesuré en S5/S9) ; budget ne laissant pas un caractère de contenu → `ChunkBudgetError`. `chunk_hash = SHA-256(contextual_text ‖ policy.version)` ; `chunk_id` = UUIDv5(version, politique, rang de l'unité, rang dans l'unité) ; la citation reprend la base de l'import (document, titre, version, numéro, fichier) avec le chemin de l'unité et les pages des blocs du chunk (I13).

**Politique de production** (`DEFAULT_CHUNKING_POLICY`, `extract_version_handler.py`) : version `kb-chunking-v1`, `max_tokens` **508** = 512 (profil `multilingual-e5-base`) − 2 tokens spéciaux (`<s>`, `</s>`) − 2 tokens du préfixe `passage: ` (mesurés sur le tokenizer réel le 2026-11-18). L'en-tête contextuel fait partie du texte compté : il n'appelle pas de marge supplémentaire. Overlap 0, fallbacks de code par défaut.

**Compteur de tokens provisoire** (`adapters/gateway/embedding/approximate_token_counter.py`, port `TokenCounter`) : approximation **par excès** du tokenizer XLM-R du profil, sans dépendance (un mot = `ceil(longueur / 3)` tokens, +1 s'il contient un caractère non ASCII ; ponctuation et `_` = 1, 2 après un blanc). Mesure contre le vrai tokenizer sur 937 paragraphes et lignes (fixtures, conception, code) : rapport estimation / réel 1,40 au global, jamais inférieur à 1,07 sur les échantillons de 50 tokens ou plus. Les chunks sont donc environ 30 % plus courts que ce que le modèle accepterait. S4 remplace ce compteur par celui de l'`Embedder` (nouvelle version de politique si les chunks changent).

**Codes d'erreur** (`work_items.last_error_code` et `document_versions.failure_code`) :

| Code | Cause | Retry de l'item | `failure_retryable` |
|---|---|---|---|
| `EMPTY_DOCUMENT` | aucun contenu (vide, blancs, front-matter seul) | non (terminal) | faux |
| `INVALID_DOCUMENT` | non UTF-8, type de média non pris en charge, conversion Docling impossible | non (terminal) | faux |
| `BLOB_MISSING` | original absent du volume : incohérence durable du stockage | non (terminal) | vrai (après restauration du blob) |
| `INVALID_PAYLOAD` | payload de l'item illisible | non (terminal) | — (aucune version désignée, rien n'est écrit) |
| autre (`TRANSIENT`, `UNEXPECTED_ERROR`…) | panne passagère | oui, avec backoff ; `FAILED` à la dernière tentative | vrai |

Tables (migration `22df253e1e05`, schéma `knowledge_base`) :

| Table | Contenu | Contraintes |
|---|---|---|
| `knowledge_units` | `id` (UUIDv5), `version_id`, `chunking_policy_version`, `ordinal`, `unit_type`, `question_text`, `section_path text[]`, `page_start`, `page_end`, `token_count`, `text` (blocs rendus, pour la reconstruction de S5), `created_at` | `UNIQUE(version_id, chunking_policy_version, ordinal)`, clé étrangère vers `document_versions`, `CHECK` sur `unit_type`, « question ssi `QA` », pages cohérentes |
| `document_chunks` | `id` (UUIDv5), `unit_id`, `version_id`, `chunking_policy_version`, `ordinal_in_unit`, `contextual_text`, `chunk_hash`, `token_count`, `citation jsonb` (clés de `Citation`), `created_at` | `UNIQUE(unit_id, ordinal_in_unit)`, clés étrangères vers `knowledge_units` et `document_versions`, index `(version_id, chunking_policy_version)` |

Données immuables, sans `updated_at` ; écrites par `SqlAlchemyExtractionRepository` (deux `INSERT` multi-lignes) dans la session de la transaction de finalisation (`SqlAlchemyExtractionTransaction`), jamais réécrites : un second lot pour la même version et la même politique viole les clés primaires.

Corpus de fixtures FR/EN (`backend/tests/fixtures/knowledge_base/`, un `.golden.json` par fixture, rejoué avec le double `MarkdownLiteParser` et avec Docling) : base de l'évaluation de S5. Les fixtures sont exclues de `ruff` (`extend-exclude`), qui formaterait les blocs de code du Markdown.

## Surface publique

`knowledge_base/__init__.py` réexporte, depuis `adapters/controller/dependencies.py` :

| Nom | Rôle |
|---|---|
| `register(app, resources)` | monte les routes d'import sur l'application FastAPI, à sa construction ; `resources` est un fournisseur de `KnowledgeBaseResources`, appelé à chaque requête (les ressources sont créées par `runtime` dans le `lifespan`) |
| `KnowledgeBaseResources(session_factory, settings)` | ressources de l'API : fabrique de sessions du runtime, `KnowledgeBaseSettings` relu à chaque construction d'application |
| `register_worker(celery_app, resources, settings=None)` | déclare les tâches `knowledge_base.work.{execute,dispatch,recover}`, leurs routes et le `beat_schedule`, dans le processus maître ; `resources` est un fournisseur appelé seulement à l'exécution d'une tâche |
| `KnowledgeBaseWorkerResources(session_factory, loop, handlers=())` | ressources d'un processus enfant : fabrique de sessions, boucle asyncio persistante, handlers (registre construit à la création) |
| `KnowledgeBaseSettings` | réglages `KB_*` (pydantic-settings) |
| `WorkHandler` | Protocol d'un handler : `kind`, `queue`, `max_attempts` (appliqué au claim), `execute(item, ctx)` ; hook facultatif `on_terminal_failure(session, item, error)` |
| `build_work_handlers(session_factory, settings=None)` | handlers de production des work items (S3 : `extract_version`), construits par `runtime` dans chaque processus enfant ; ne se connecte à rien |

`runtime` n'importe que cette racine (`backend/tests/architecture/`). `alembic/env.py` importe `knowledge_base.frameworks.sqlalchemy.metadata` (exception documentée dans [005](../../../docs/conception/005-project-structure.md#transmission-dun-contrat-public-à-un-autre-contexte)).

## Organisation

```text
knowledge_base/
  domain/entities/knowledge_document.py   # KnowledgeDocument, DocumentVersion, DocumentStatus, VersionStatus (S1)
  domain/value_objects/                   # ContentHash, DocumentSource, DocumentKind, FailureReason (S1)
  domain/events/document_events.py        # Domain Events de KnowledgeDocument (S1)
  domain/exceptions/document_errors.py    # KnowledgeDocumentError et ses sous-classes (S1)
  domain/providers/work_queue.py          # Protocol WorkQueue (enqueue, cancel), FailureInfo, codes d'erreur stables
  domain/providers/                       # S2 : unit_of_work, knowledge_document_repository, idempotency_key_repository, document_blob_store
                                          # S3 : document_parser, document_blob_reader, token_counter, extraction_repository
  domain/value_objects/                   # S3 : structured_document, section_path, page_span, citation, chunking_policy, knowledge_unit, document_chunk
  domain/exceptions/parsing_errors.py     # DocumentParseError, EMPTY_DOCUMENT, INVALID_DOCUMENT (S3)
  domain/services/                        # knowledge_unit_segmenter, chunk_planner, identifiers (UUIDv5) (S3)
  use_cases/import_document/              # ImportDocument, ImportDocumentHandler, ImportDocumentResult (S2)
  use_cases/import_document_version/      # ImportDocumentVersion, ImportDocumentVersionHandler (S2)
  use_cases/import_steps.py               # liste blanche, empreinte de requête, idempotence, enqueue / cancel, rejeu après course
  use_cases/errors.py                     # UnsupportedDocumentType, DocumentNotFound, DuplicateContentOtherDocument, IdempotencyKeyConflict
  use_cases/record_extraction/            # RecordExtraction, RecordExtractionHandler (prepare, apply, fail), PreparedExtraction (S3)
  adapters/failure_policy.py              # FailureDecision, decide_recovery — purs, partagés controller / gateway
  adapters/controller/dependencies.py     # register, register_worker et réexports de la surface publique
  adapters/controller/rest/               # routes d'import, schémas HTTP, traduction des refus, KnowledgeBaseResources (S2)
  adapters/controller/tasks/
    execution.py                          # execute_work_item, WorkContext (heartbeat, finalize)
    dispatch.py                           # dispatch_pending, recover_expired_leases
    registry.py                           # WorkHandler, HandlerRegistry
    resources.py                          # KnowledgeBaseWorkerResources
    retry_policy.py                       # backoff (equal jitter), classification des erreurs — purs
    extract_version_handler.py            # ExtractVersionWorkHandler, build_extract_version_handler, DEFAULT_CHUNKING_POLICY (S3)
  adapters/gateway/persistence/
    sqlalchemy_work_queue.py              # SqlAlchemyWorkQueue (port), SqlAlchemyWorkLifecycle (transitions §3)
    sqlalchemy_unit_of_work.py            # SqlAlchemyUnitOfWork (S2)
    sqlalchemy_knowledge_document_repository.py, sqlalchemy_idempotency_key_repository.py
    constraint_violations.py              # nom de la contrainte violée (traduction des IntegrityError attendues)
    sqlalchemy_extraction_repository.py, sqlalchemy_extraction_transaction.py  # lot d'unités et chunks (S3)
  adapters/gateway/blob/filesystem_blob_store.py  # FileSystemBlobStore : put (S2), read (S3)
  adapters/gateway/parsing/               # RoutingDocumentParser, DoclingMarkdownParser, PlainTextParser (S3)
  adapters/gateway/embedding/approximate_token_counter.py  # TokenCounter provisoire (S3, remplacé en S4)
  frameworks/sqlalchemy/tables.py         # MetaData(schema="knowledge_base"), work_items, documents, document_versions, idempotency_keys, knowledge_units, document_chunks
  frameworks/configuration/settings.py    # KnowledgeBaseSettings
```

## Écrire un handler (slices S2 et suivantes)

1. Effets externes **idempotents**, hors transaction ; `await ctx.heartbeat()` au moins toutes les `ctx.heartbeat_interval` pour un traitement long ; travail CPU dans `asyncio.to_thread`.
2. `await ctx.finalize(callback)` : `callback(session)` écrit l'état métier ; le passage `SUCCEEDED` est ajouté dans la même transaction, conditionné au fencing token. Un token périmé annule toute la transaction (`StaleAttempt`).
3. Erreurs : `TerminalWorkError(code, message)` → `FAILED` ; `TransientWorkError` et toute autre exception → nouvelle tentative avec backoff tant que `attempts < max_attempts`. Un handler qui se termine sans `ctx.finalize` est un échec terminal `NOT_FINALIZED` (le hook est appelé).
4. Aucune écriture PostgreSQL métier hors de `ctx.finalize` (ou du hook `on_terminal_failure`, exécuté dans la transaction de `fail`).
5. Si `on_terminal_failure` lève : la transaction de `fail` est annulée (aucune écriture du hook), puis nouvelle tentative avec backoff et le code `TERMINAL_HOOK_FAILED` ; à la dernière tentative, `FAILED` (`TERMINAL_HOOK_FAILED`) **sans** hook. L'état métier de ce cas, comme celui d'un `FAILED` par `recover`, est le point ouvert O14 ([knowledge-base.md §16](../../../docs/conception/bounded-contexts/knowledge-base.md#16-points-ouverts)).

`max_attempts` : `enqueue(max_attempts=None)` applique le défaut de la colonne (5) ; le **claim** remplace cette valeur par celle du handler du `kind`, dans le même `UPDATE`. `retry_later` et `recover` lisent la colonne.

En S0, `finalize` et `on_terminal_failure` reçoivent une `AsyncSession` ; l'Unit of Work du contexte (`SqlAlchemyUnitOfWork`) existe depuis S2, côté API.

`FailureInfo` et `FailureReason` restent distincts (pas la même forme) :

- `FailureInfo` (`domain/providers/work_queue.py`, code stable + message tronqué à 2 000 caractères) décrit l'échec d'une **tentative** de work item : colonnes `last_error_*` et argument du hook `on_terminal_failure` (S0 §5) ;
- `FailureReason` (`domain/value_objects/failure_reason.py`, `code`, `message`, `retryable`) est l'état **durable** d'une version `FAILED` ; `retryable` dit si `RetryIngestion` a un sens.

Le handler `extract_version` traduit l'un en l'autre dans son hook (`retryable` faux pour `EMPTY_DOCUMENT` et `INVALID_DOCUMENT`) ; une fusion ne sera envisagée que si les deux formes convergent.

## Choix d'implémentation à connaître

- **Plafond en vol par file** (`KB_WORK_MAX_IN_FLIGHT`, appliqué à chaque file de handlers) : `RUNNING` + `PENDING` notifiés depuis moins de `redispatch_after`, tous `kind` de la file confondus. Il borne approximativement la file : le comptage n'est pas verrouillé, deux `dispatch` concurrents peuvent dépasser légèrement le plafond (sans danger grâce au claim).
- **Publication hors boucle asyncio** : la tâche `dispatch` collecte les notifications pendant `dispatch_pending` (qui commit), puis publie avec Kombu (synchrone) une fois la coroutine terminée.
- **`recover`** reprend au plus `KB_WORK_DISPATCH_BATCH` leases par passage ; l'adapter SQL applique `decide_recovery` (`adapters/failure_policy.py`), seule source de la décision, sans dépendre du controller.
- **`worker_prefetch_multiplier = 1`** est un réglage du processus worker, posé par `runtime` (et répété sur la ligne de commande de `worker_ingestion`), pas par `register_worker`.
- Les tâches sont enregistrées avec `shared=False` : sans cela, Celery les ajoute à toute application créée ensuite dans le processus, avec les réglages et ressources de la première.

## Tests

- Unitaires : `backend/tests/unit/knowledge_base/` (backoff, classification, registre, settings, `register_worker`) ; `domain/` pour S1 (import, transitions, publication, retrait, événements, Value Objects), sans I/O.
- Unitaires S2 : `use_cases/` (handlers avec fakes des ports : dédoublonnage, I6, enqueue, idempotence, courses), `adapters/test_filesystem_blob_store.py`, réglages et montage des routes.
- Unitaires S3 : `domain/test_extraction_value_objects.py`, `domain/services/` (segmentation Q/R, sections, définitions, identifiants, `ChunkPlanner` et ses fallbacks), `adapters/parsing/` (TXT, routage, Docling), `adapters/test_approximate_token_counter.py`, `use_cases/test_record_extraction.py`, `test_extract_version_handler.py`, `test_extract_version_lease_and_blob.py` (heartbeats pendant le calcul, `BLOB_MISSING`), `test_corpus_golden.py` (goldens rejoués avec Docling) ; `architecture/test_docling_confinement.py`.
- Intégration (`make test-integration`) : `backend/tests/integration/knowledge_base/`, scénarios 1 à 14 de S0 §12.2 contre PostgreSQL et RabbitMQ réels ; worker in-process (pool solo) et vrai worker prefork en sous-processus (scénarios 13 et 14). S2 : `test_import_api` (POST multipart réel → lignes, blob, work item), `test_import_atomicity` (orphelin après rollback, verrou optimiste, course sur un même nouveau fichier avec barrière explicite), `test_import_persistence`, `test_import_migration` ; le volume de blobs est un répertoire temporaire (`KB_BLOB_ROOT`). S3 : `test_extraction_flow` (import réel → `EXTRACTED`, unités, chunks, citations, Docling réel), `test_extraction_fencing`, `test_extraction_failures`, `test_extraction_rerun` (mêmes identifiants après un crash), `test_extraction_blob_missing`, `test_extraction_migration`.
