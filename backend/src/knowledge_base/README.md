# Knowledge Base

Bounded Context ([ADR 0007](../../../docs/decisions/0007-knowledge-base-bounded-context.md)) chargé des documents de connaissances, de leur ingestion (extraction, chunking, embeddings) et de la recherche sémantique publiée vers les autres contextes. Conception : [knowledge-base.md](../../../docs/conception/bounded-contexts/knowledge-base.md) ; feuille de route : [S0–S9](../../../docs/conception/bounded-contexts/knowledge-base-roadmap.md).

## État : slices S0, S1 et S2

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

Un échec de la transaction laisse un blob orphelin (purgé par la réconciliation S7/S8), jamais une ligne sans blob. L'item `extract_version` reste `PENDING` jusqu'à S3 : le dispatcher ignore les `kind` sans handler.

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
  use_cases/import_document/              # ImportDocument, ImportDocumentHandler, ImportDocumentResult (S2)
  use_cases/import_document_version/      # ImportDocumentVersion, ImportDocumentVersionHandler (S2)
  use_cases/import_steps.py               # liste blanche, empreinte de requête, idempotence, enqueue / cancel, rejeu après course
  use_cases/errors.py                     # UnsupportedDocumentType, DocumentNotFound, DuplicateContentOtherDocument, IdempotencyKeyConflict
  adapters/failure_policy.py              # FailureDecision, decide_recovery — purs, partagés controller / gateway
  adapters/controller/dependencies.py     # register, register_worker et réexports de la surface publique
  adapters/controller/rest/               # routes d'import, schémas HTTP, traduction des refus, KnowledgeBaseResources (S2)
  adapters/controller/tasks/
    execution.py                          # execute_work_item, WorkContext (heartbeat, finalize)
    dispatch.py                           # dispatch_pending, recover_expired_leases
    registry.py                           # WorkHandler, HandlerRegistry
    resources.py                          # KnowledgeBaseWorkerResources
    retry_policy.py                       # backoff (equal jitter), classification des erreurs — purs
  adapters/gateway/persistence/
    sqlalchemy_work_queue.py              # SqlAlchemyWorkQueue (port), SqlAlchemyWorkLifecycle (transitions §3)
    sqlalchemy_unit_of_work.py            # SqlAlchemyUnitOfWork (S2)
    sqlalchemy_knowledge_document_repository.py, sqlalchemy_idempotency_key_repository.py
    constraint_violations.py              # nom de la contrainte violée (traduction des IntegrityError attendues)
  adapters/gateway/blob/filesystem_blob_store.py  # FileSystemBlobStore (S2)
  frameworks/sqlalchemy/tables.py         # MetaData(schema="knowledge_base"), work_items, documents, document_versions, idempotency_keys
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

Le handler d'extraction qui posera `FAILED` aura à traduire l'un en l'autre ; une fusion ne sera envisagée que si les deux formes convergent.

## Choix d'implémentation à connaître

- **Plafond en vol par file** (`KB_WORK_MAX_IN_FLIGHT`, appliqué à chaque file de handlers) : `RUNNING` + `PENDING` notifiés depuis moins de `redispatch_after`, tous `kind` de la file confondus. Il borne approximativement la file : le comptage n'est pas verrouillé, deux `dispatch` concurrents peuvent dépasser légèrement le plafond (sans danger grâce au claim).
- **Publication hors boucle asyncio** : la tâche `dispatch` collecte les notifications pendant `dispatch_pending` (qui commit), puis publie avec Kombu (synchrone) une fois la coroutine terminée.
- **`recover`** reprend au plus `KB_WORK_DISPATCH_BATCH` leases par passage ; l'adapter SQL applique `decide_recovery` (`adapters/failure_policy.py`), seule source de la décision, sans dépendre du controller.
- **`worker_prefetch_multiplier = 1`** est un réglage du processus worker, posé par `runtime` (et répété sur la ligne de commande de `worker_ingestion`), pas par `register_worker`.
- Les tâches sont enregistrées avec `shared=False` : sans cela, Celery les ajoute à toute application créée ensuite dans le processus, avec les réglages et ressources de la première.

## Tests

- Unitaires : `backend/tests/unit/knowledge_base/` (backoff, classification, registre, settings, `register_worker`) ; `domain/` pour S1 (import, transitions, publication, retrait, événements, Value Objects), sans I/O.
- Unitaires S2 : `use_cases/` (handlers avec fakes des ports : dédoublonnage, I6, enqueue, idempotence, courses), `adapters/test_filesystem_blob_store.py`, réglages et montage des routes.
- Intégration (`make test-integration`) : `backend/tests/integration/knowledge_base/`, scénarios 1 à 14 de S0 §12.2 contre PostgreSQL et RabbitMQ réels ; worker in-process (pool solo) et vrai worker prefork en sous-processus (scénarios 13 et 14). S2 : `test_import_api` (POST multipart réel → lignes, blob, work item), `test_import_atomicity` (orphelin après rollback, verrou optimiste, course sur un même nouveau fichier avec barrière explicite), `test_import_persistence`, `test_import_migration` ; le volume de blobs est un répertoire temporaire (`KB_BLOB_ROOT`).
