# Knowledge Base

Bounded Context ([ADR 0007](../../../docs/decisions/0007-knowledge-base-bounded-context.md)) chargé des documents de connaissances, de leur ingestion (extraction, chunking, embeddings) et de la recherche sémantique publiée vers les autres contextes. Conception : [knowledge-base.md](../../../docs/conception/bounded-contexts/knowledge-base.md) ; feuille de route : [S0–S9](../../../docs/conception/bounded-contexts/knowledge-base-roadmap.md).

## État : slices S0 à S5

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

Les deux rendent `ImportDocumentResult(document_id, version_id, number, created)` (`use_cases/import_result.py`). Déroulé :

1. contrôle du nom de fichier et du type **avant toute écriture** : le nom est réduit à son nom final (tout chemin `/` ou `\` retiré : `..\..\notes/cours.md` → `cours.md`), refusé s'il est vide (`dossier/`, `..`), dépasse 255 caractères ou contient un caractère de contrôle (`InvalidFilenameError`) ; liste blanche type de média + extension (`text/markdown` : `.md`, `.markdown` ; `text/plain` : `.txt`), paramètres (`; charset=…`) retirés du type stocké ;
2. dépôt du blob **hors transaction** (`DocumentBlobStore.put`) : SHA-256 au fil du flux, limite `KB_MAX_UPLOAD_BYTES` appliquée pendant la lecture ; un fichier vide est accepté (il échouera à l'extraction, S3) ;
3. **une** transaction (`UnitOfWork`) : rejeu éventuel de la clé d'idempotence, document / version, item `extract_version` (`dedup_key = extract_version:<version_id>`, payload `document_id`, `version_id`), annulations d'I6, clé d'idempotence, `commit`.

Un échec de la transaction laisse un blob orphelin (purgé par la réconciliation S7/S8), jamais une ligne sans blob. L'item `extract_version` est traité depuis S3 (ci-dessous).

- **Dédoublonnage et O2** ([knowledge-base.md §16](../../../docs/conception/bounded-contexts/knowledge-base.md#16-points-ouverts)) : `UNIQUE(content_hash)` global. Même contenu sans cible → document existant, quelles que soient les métadonnées ; version ciblée dont le contenu appartient à un autre document → `DuplicateContentOtherDocumentError` ; même document → `DuplicateContentError` (I3). Le contenu d'un document retiré ne peut pas être réimporté (`DocumentWithdrawnError`).
- **Idempotence** (002) : `Idempotency-Key` facultatif. Empreinte de requête = SHA-256 d'un JSON canonique (opération, document cible, titre, nature, thèmes, langue, nom de fichier réduit, type de média, empreinte du contenu). Même clé et même empreinte → résultat enregistré, sans ré-exécution (même `created`) ; empreinte différente → `IdempotencyKeyConflictError`. La clé est écrite dans la transaction de l'import. Les clés n'expirent pas encore (point ouvert O15).
- **Concurrence** : deux versions concurrentes d'un même document → verrou optimiste (`documents.version`), le perdant lève `ConcurrentModificationError`. Deux imports simultanés du même nouveau contenu, ou de la même clé → l'adapter traduit la violation d'unicité en `ContentHashTakenError` / `IdempotencyKeyTakenError` (aucune `IntegrityError` ne sort de la persistance). Dans les trois cas, le handler rejoue **une fois** sa transaction, qui relit tout : même clé → réponse du gagnant (même résultat), même contenu sans clé → `DUPLICATE_CONTENT` (ou document existant), autre contenu → import sur l'état à jour. Une seconde course perdue remonte (409 `CONCURRENT_MODIFICATION`).

Routes (`adapters/controller/rest/`, montées par `register`) :

| Méthode et chemin | Corps (multipart) | Succès |
|---|---|---|
| `POST /api/v1/knowledge-base/documents` | `file`, `title`, `kind` (`COURSE`, `QA_SHEET`, `REVISION_SHEET`, `TECHNICAL`), `language`, `topics` (répétable, facultatif) | 202 `{document_id, version_id, number}` ; 200, même corps, si le contenu était déjà connu |
| `POST /api/v1/knowledge-base/documents/{document_id}/versions` | `file` | 202 `{document_id, version_id, number}` |

En-tête facultatif `Idempotency-Key` (1 à 255 caractères). Erreurs `{code, message, ...}` :

| HTTP | `code` | Champs complémentaires | Cause |
|---|---|---|---|
| 422 | `INVALID_FILENAME` | `reason` | nom de fichier vide une fois réduit à son nom final, de plus de 255 caractères ou avec un caractère de contrôle ; ni blob ni ligne |
| 413 | `PAYLOAD_TOO_LARGE` | `max_bytes` | fichier au-delà de `KB_MAX_UPLOAD_BYTES` ; ni blob ni ligne |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | `filename`, `media_type` | couple type / extension hors liste blanche |
| 404 | `DOCUMENT_NOT_FOUND` | `document_id` | document cible inconnu |
| 409 | `DUPLICATE_CONTENT` | `document_id`, `version_id` (version existante) | contenu déjà présent dans ce document (I3) ou dans un autre (O2) |
| 409 | `IDEMPOTENCY_KEY_CONFLICT` | — | même clé, autre requête |
| 409 | `DOCUMENT_WITHDRAWN` | `document_id` | document retiré (nouvelle version ou réimport de son contenu) |
| 409 | `CONCURRENT_MODIFICATION` | — | course perdue deux fois de suite (la première est rejouée par le handler) ; le client rejoue |
| 422 | format FastAPI (`detail`) | — | formulaire invalide (champ manquant, `kind` inconnu, identifiant non UUID) |

Le fichier n'est jamais lu en entier en mémoire : Starlette stocke la partie multipart dans un fichier temporaire (en mémoire jusqu'à 1 Mio, sur disque au-delà) **avant** l'appel de la route, puis le routeur la transmet au blob store par morceaux de 64 Kio. Pic mesuré (`tracemalloc`) pour un dépôt de 20 Mio : environ 120 Kio. La limite ne s'applique donc qu'après la réception complète par Starlette ; borner le corps HTTP lui-même relève du serveur ou du proxy.

Stockage (`adapters/gateway/blob/filesystem_blob_store.py`, ADR 0012) : flux écrit sous `KB_BLOB_ROOT/tmp/`, `fsync`, puis `os.replace` vers `KB_BLOB_ROOT/sha256/<aa>/<bb>/<empreinte>` et `fsync` du répertoire ; aucun fichier n'apparaît au chemin final avant la fin du flux ; écriture idempotente, y compris pour des dépôts concurrents du même contenu ; I/O disque via `asyncio.to_thread`.

Tables (migration `65b08637988e`, schéma `knowledge_base`) :

| Table | Contenu | Contraintes |
|---|---|---|
| `documents` | métadonnées de l'agrégat, `topics text[]`, `status`, `published_version_id`, `visibility_revision`, `version` (verrou optimiste) | `CHECK` sur `status` et `kind` ; pas de clé étrangère vers les versions (référence circulaire) |
| `document_versions` | `number`, `status`, `filename`, `media_type`, `size_bytes`, `content_hash`, `imported_at`, `failure_*` | `UNIQUE(content_hash)` (I3 + O2), `UNIQUE(document_id, number)` (I2), clé étrangère vers `documents`, `CHECK` sur `status` et « `failure_*` renseignés ssi `FAILED` », index unique partiel « une `PUBLISHED` par document » (I4) |
| `idempotency_keys` | `key` (clé primaire), `fingerprint`, résultat (`document_id`, `version_id`, `number`, `created`) | sans clé étrangère : un enregistrement rend un résultat passé |

Persistance (`adapters/gateway/persistence/`) : `SqlAlchemyUnitOfWork(session_factory)` — une session et une transaction par `async with`, partagées par le dépôt du document, `SqlAlchemyWorkQueue` (S0) et le dépôt des clés. `save(document)` écrit le document **avant** ses versions (`UPDATE … WHERE version = <lue>`, zéro ligne → `ConcurrentModificationError`), puis seulement les versions nouvelles ou modifiées par rapport à l'état lu dans la transaction. Dans une Unit of Work, la **première** lecture d'un document fait foi : une relecture (`get`) ne rafraîchit ni la version du verrou ni l'état de référence, donc sauver l'instance lue en premier après un commit concurrent lève `ConcurrentModificationError` (pas de lost update). `find_by_content_hash` rend un modèle de lecture `ContentOwner(document_id, document_status, version_id, version_number)`, pas l'agrégat.

I1 en base (migration `756db53032ab`) : le trigger `BEFORE UPDATE` `trg_document_versions_source_immutable` refuse toute modification de `number`, `content_hash`, `filename`, `media_type`, `size_bytes`, `imported_at` (`check_violation`) ; seuls l'état et `failure_*` changent. Fonction et trigger hors MetaData : `alembic check` ne les compare pas.

Assemblage (`adapters/controller/dependencies.py`) : `register` construit les handlers d'import (`build_import_document_handler`, `build_import_version_handler`) et les passe en fabriques à `add_document_routes(router, *, import_document_handler, import_version_handler)` ; le routeur ne connaît ni SQLAlchemy ni le blob store, et un test peut substituer les fabriques (`tests/unit/knowledge_base/test_document_routes_http.py`, sans base).

### S3 — Docling Markdown, `KnowledgeUnit` et chunking

Une version `RECEIVED` devient `EXTRACTED` avec ses unités et chunks, par le handler du `kind` `extract_version` (file `knowledge_base.ingestion`, `max_attempts` 5), enregistré en production par `build_work_handlers` ([knowledge-base.md §3.4, §6](../../../docs/conception/bounded-contexts/knowledge-base.md#34-unités-et-chunks--données-dérivées-immuables-accepted), [ADR 0008](../../../docs/decisions/0008-custom-pipeline-docling-extraction.md), [ADR 0009](../../../docs/decisions/0009-pedagogical-chunking-knowledge-units.md)).

**Pipeline** (`adapters/controller/tasks/extract_version_handler.py` → `use_cases/record_extraction/`) :

1. `RecordExtractionHandler.prepare`, **hors transaction** : relecture de l'agrégat (Unit of Work brève, sans commit) ; rien à faire si le document est absent ou retiré, ou si la version n'est plus `RECEIVED` ; lecture du blob ; puis **dans un thread** (`asyncio.to_thread`) : `DocumentParser` → `StructuredDocument`, `KnowledgeUnitSegmenter` (selon `document.kind`), `ChunkPlanner`. Pendant ce calcul, le handler prolonge le lease (`ctx.heartbeat()` toutes les `KB_WORK_HEARTBEAT_INTERVAL_SECONDS`, un tiers du lease par défaut) ; un heartbeat refusé arrête la tentative sans finalisation.
2. Un seul `ctx.finalize` : `apply` relit l'agrégat **dans la transaction de finalisation** ; si la version est toujours `RECEIVED` et le document `ACTIVE` : `record_extraction`, `save` (verrou optimiste), puis le lot `(version_id, chunking_policy_version)` ; sinon rien n'est écrit et l'item se termine `SUCCEEDED` (version supplantée ou document retiré entre-temps : choix S3, ce n'est pas une erreur). Aucune `Indexation` en S3.
3. Échec terminal : le hook `on_terminal_failure` passe la version `RECEIVED` → `FAILED` avec `FailureReason(code, message, retryable)` ; sans effet si la version a changé d'état.

**Parseurs** (`adapters/gateway/parsing/`, port `DocumentParser`, synchrone) : `RoutingDocumentParser` aiguille sur le type de média (sans paramètre ni casse) ; `text/markdown` → `DoclingMarkdownParser`, `text/plain` → `PlainTextParser` (adapter natif, point O7). Docling n'est importé que par `docling_markdown_parser.py` (test d'architecture `test_docling_confinement`), et seulement à la première conversion : l'API compose le handler sans charger Docling.

- Markdown : UTF-8 (BOM toléré) ; **front-matter lu avant Docling** (`---` / `clé: valeur` / `---`) → `metadata`, jamais en blocs ; le bloc n'est un front-matter que si chaque ligne est `clé: valeur` (clé `[\w.-]+`), vide ou commentaire `#`, sinon le document est rendu tel quel à Docling ; guillemets entourant une valeur retirés ; imbrication trop profonde (`RecursionError` de Docling) → `DOCUMENT_TOO_COMPLEX`, parcours de l'arbre Docling itératif ; niveaux de titres restitués (Docling rend `#` en `TitleItem`, `##` en `SectionHeaderItem(level=1)`) ; paragraphe à mise en forme recollé (code en ligne entre accents graves, retours à la ligne → espaces) ; code avec le langage reconnu par Docling en minuscules (`None` si inconnu) ; listes aplaties ; tableau → une ligne `| a | b |` par rangée ; pas de page.
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

Puis, pour tous, les mots, et en dernier recours les caractères : le budget est garanti quel que soit le contenu. **Choix S3 pour les cas non figés par les tests de l'auteur** : paragraphe ou liste seuls trop grands → phrases / éléments puis mots (recollés par une espace : les retours à la ligne internes d'un paragraphe découpé sont perdus) ; **en-tête de plus de la moitié du budget → tronqué aux mots et suivi de ` …`** (la moitié du budget reste au contenu) ; `overlap_tokens > 0` → `ValueError` (mesuré en S5/S9) ; budget ne laissant pas un caractère de contenu → `ChunkBudgetError`. `chunk_hash = SHA-256(contextual_text ‖ "\x00" ‖ policy.identity)` ; `unit_id` et `chunk_id` = UUIDv5(version, `policy.identity`, rangs), où `identity = version+tokenizer` (valeur persistée dans `chunking_policy_version`). Chaque recherche du plus long préfixe qui tient procède par bonds doublés puis dichotomie : découpe linéaire (2 Mo sans blanc : 238 s → 2,5 s, mesuré le 2026-11-18) ; la citation reprend la base de l'import (document, titre, version, numéro, fichier) avec le chemin de l'unité et les pages des blocs du chunk (I13).

**Politique de production en S3** (`DEFAULT_CHUNKING_POLICY`, `frameworks/configuration/chunking.py`) : version `kb-chunking-v1`, tokenizer `approx-xlmr-v1` (remplacé en S4 par le tokenizer réel, voir S4), identité persistée `kb-chunking-v1+approx-xlmr-v1`, `max_tokens` **508** = 512 (profil `multilingual-e5-base`) − 2 tokens spéciaux (`<s>`, `</s>`) − 2 tokens du préfixe `passage: ` (mesurés sur le tokenizer réel le 2026-11-18). L'en-tête contextuel fait partie du texte compté : il n'appelle pas de marge supplémentaire. Overlap 0, fallbacks de code par défaut.

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

### S4 — embeddings, Qdrant, index initial et publication

Une version `EXTRACTED` est encodée, écrite dans Qdrant, vérifiée, puis publiée ; la synchronisation rend ensuite ses points visibles ([knowledge-base.md §6, §8–§11, §13](../../../docs/conception/bounded-contexts/knowledge-base.md#6-workflow-de-traitement-avec-work_items), [ADR 0010](../../../docs/decisions/0010-postgresql-source-of-truth-qdrant-derived-index.md), [ADR 0011](../../../docs/decisions/0011-embedding-profile-search-index-blue-green.md)).

```text
extract_version ──► EXTRACTED + Indexation PENDING (par index ACTIVE/BUILDING) + item index_version
index_version   ──► embeddings, upserts visible=false, relecture du manifest  (hors transaction)
                ──► Indexation COMPLETED (I8) + tentative de publication (I5, I9) + item sync_visibility
sync_visibility ──► visible=true pour la version publiée ; points SUPERSEDED/DISCARDED supprimés
```

**Domaine** (Python pur) :

| Élément | Rôle |
|---|---|
| `EmbeddingProfile`, `Distance` (`domain/value_objects/embedding_profile.py`) | modèle, révision, dimension, distance, normalisation, préfixes, `max_tokens`, tokenizer ; `MULTILINGUAL_E5_BASE` est le profil initial (révisable, ADR 0011 §6) |
| `KnowledgeCorpus`, `SearchIndex`, `IndexStatus`, `CORPUS_ID` (`domain/entities/knowledge_corpus.py`) | singleton V1 ; `provision_index` (`BUILDING`), `activate_index` (I10 : l'ancien actif devient `RETIRED` ; `coverage_complete` = contrôle I11 calculé par le use case), `retire_index`, `live_indexes` ; refus `corpus_errors` sans effet |
| `Indexation`, `IndexationStatus` (`domain/entities/indexation.py`) | `PENDING → COMPLETED` (digest et nombre de points), `FAILED`, `CANCELLED`, `retry` (§5.3) ; refus `InvalidIndexationTransitionError` sans effet |
| `IndexManifest` (`domain/value_objects/index_manifest.py`) | couples `(point_id, chunk_hash)` triés par `str(point_id)`, sans doublon ; `digest` = SHA-256 des lignes `"<point_id>:<chunk_hash>\n"` |
| `point_id(index_id, chunk_id)` (`domain/services/point_id.py`) | `uuid5(index_id, str(chunk_id))` : même chunk → même point dans un index, autre point dans un autre |
| `IndexedPoint`, `PointSelector` | point à écrire (payload §9, sans `visible` : l'adapter Qdrant écrit toujours `visible=false`, seul `set_visibility` le rend vrai ; champ retiré à la revue S5) ; points d'une version ou de tout un document |

Ports (`domain/providers/`) : `Embedder`, `VectorIndex`, `ChunkReader`, `KnowledgeCorpusRepository` (`get_shared` = `FOR SHARE`, `get_for_update` = `FOR UPDATE`, `add` lève `CorpusAlreadyExistsError`), `IndexationRepository`, `IndexingTransaction` (documents, corpus, indexations, work queue liés à une transaction ouverte par l'appelant) et `IndexingUnitOfWork` (la même, ouverte par le handler, plus les chunks) ; `ExtractionTransaction` étend `IndexingTransaction`.

**Use cases** :

- `RecordExtraction.apply` (S3, étendu) : dans la transaction qui passe la version `EXTRACTED`, lit le corpus sous **`FOR SHARE`** et crée, pour chaque index `ACTIVE` ou `BUILDING` dont la politique est celle du lot, une `Indexation PENDING` et un item `index_version` (dédoublonnage `index_version:<version_id>:<index_id>`). Sans corpus : aucune indexation (la version reste `EXTRACTED`). Index d'une autre politique : ignoré avec un avertissement (rechunking, S9). Une version passée `EXTRACTED` sans **aucune** indexation est journalisée en `WARNING` avec le code `EXTRACTED_WITHOUT_INDEXATION` (constante de `record_extraction_handler.py`, dans le message `code=… document_id=… version_id=…` et en attribut `code` de l'enregistrement) : repère de la réconciliation S8.
- `CompleteIndexation` (`use_cases/complete_indexation/`) : `prepare` lit document, corpus, indexation et chunks dans une transaction brève, **la ferme**, puis vérifie la collection (une fois : modèle chargé = profil de l'index, collection présente avec la dimension et la distance du profil), encode par lots de `KB_EMBEDDING_BATCH_SIZE` chunks, écrit chaque lot (`upsert`, `wait=true`, points `visible=false`), relit les points par identifiants (lots de 256, sans vecteurs) et exige l'égalité exacte des couples `(point_id, chunk_hash)`. `apply` (transaction de finalisation) relit l'état : indexation toujours `PENDING`, version `EXTRACTED` ou `PUBLISHED`, document `ACTIVE`, index non `RETIRED` → `COMPLETED`, puis **tentative** de publication (un refus laisse l'indexation `COMPLETED`) ; sinon l'indexation passe `CANCELLED`. `fail` : `PENDING → FAILED`.
- `PublishDocumentVersion` (`apply` seulement, dans la transaction fournie : celle de la finalisation de `CompleteIndexation` ; aucune transaction propre, faute d'appelant) : l'index actif est lu sous **`FOR SHARE`** du corpus ; I9 (indexation `COMPLETED` dans **cet** index), puis l'agrégat (version `EXTRACTED`, document `ACTIVE`, I5) ; succès : item `sync_visibility` (`sync_visibility:<document_id>:<visibility_revision>`). Refus : `NoActiveIndexError`, `IndexationNotCompletedError` (`PublicationRefusedError`) ou `KnowledgeDocumentError`, toujours avant écriture.
- `SyncDocumentVisibility` : lit l'état SQL **courant** (transaction fermée avant Qdrant) ; dans chaque index non `RETIRED` : document retiré → tous ses points supprimés ; sinon version publiée → `visible=true`, versions `SUPERSEDED`/`DISCARDED` → points supprimés. Les versions `EXTRACTED` ne sont pas touchées (précision S4 de §11). Idempotent, sans écriture SQL.
- `ProvisionSearchIndex` : si le corpus a un index actif, le rend sans rien écrire ; sinon crée la collection **avant** la transaction, puis crée le corpus si besoin, provisionne et active l'index (`coverage_complete=True` : corpus vide), `commit`. Un corpus créé entre-temps par un autre processus (`CorpusAlreadyExistsError`) : l'index actif du gagnant est rendu.

**Handlers de work items** (file `knowledge_base.ingestion`, `max_attempts` 5) :

| `kind` | Exécution | Erreurs |
|---|---|---|
| `index_version` | `prepare` (heartbeats pendant l'encodage, qui tourne dans un thread ; un heartbeat refusé annule `prepare` avant l'upsert suivant), puis un `ctx.finalize` : `apply`, ou `cancel_if_obsolete` s'il n'y avait rien à indexer | `IndexManifestMismatchError` → transitoire (la reprise réécrit) ; `CollectionMismatchError` → terminal `COLLECTION_MISMATCH`, indexation `FAILED` (`retryable` vrai) ; payload illisible → `INVALID_PAYLOAD` ; autre → transitoire, `FAILED` à la dernière tentative |
| `sync_visibility` | `SyncDocumentVisibility.handle`, puis un `ctx.finalize` qui n'écrit rien (fencing et `SUCCEEDED`) | payload illisible → `INVALID_PAYLOAD` ; Qdrant indisponible → transitoire |

Un échec d'indexation laisse la version `EXTRACTED` : ses chunks restent valides (§5.2).

**Adapters** :

- `SentenceTransformersEmbedder` (`adapters/gateway/embedding/sentence_transformers_embedder.py`) : seul module qui importe `sentence_transformers` (donc torch), et seulement dans `_load` (`tests/architecture/test_embedding_confinement.py`). Préfixes et normalisation du profil, encodage dans `asyncio.to_thread`, appareil `cpu` imposé (pas de MPS implicite sous macOS). `token_counter` = tokenizer du modèle, sans préfixe ni tokens spéciaux. `load(profile, cache_dir=…)` charge tout de suite ; `lazy(…)` charge au premier encodage **ou** au premier comptage de tokens, une seule fois (verrou).
- `QdrantVectorIndex` (`adapters/gateway/vector_store/qdrant_vector_index.py`) : collection `kb_index_<index_id hex>` par index ; création idempotente avec les index de payload `visible` (booléen), `unit_id`, `document_id`, `version_id`, `topics`, `kind`, `language` (keyword) ; toutes les écritures avec `wait=true` et statut `completed` exigé (`VectorWriteNotAppliedError` sinon, transitoire) ; `retrieve` sans vecteurs (payload `chunk_hash` seul) ; visibilité et suppressions par filtre `document_id` (+ `version_id`).
- Persistance : `SqlAlchemyKnowledgeCorpusRepository` (`ON CONFLICT DO NOTHING` à la création, verrou optimiste sur `version`, index non actifs écrits avant l'actif), `SqlAlchemyIndexationRepository`, `SqlAlchemyChunkReader`, `SqlAlchemyIndexingTransaction` (documents, work queue, corpus, indexations ; dont héritent `SqlAlchemyExtractionTransaction` et, depuis la revue S4, `SqlAlchemyUnitOfWork`, qui ouvre et valide sa propre session et ajoute les clés d'idempotence et les chunks : les dépôts ne sont composés qu'à un endroit).

**Profil et politique de production** :

| | Valeur |
|---|---|
| Modèle | `intfloat/multilingual-e5-base`, révision `d128750597153bb5987e10b1c3493a34e5a4502a` (relevée sur le Hub le 2026-11-18) |
| Vecteurs | 768 dimensions, cosine, normalisés ; préfixes `query: ` / `passage: ` ; 512 tokens |
| Tokenizer | XLM-R livré avec le modèle, nommé `xlmr-d1287505` |
| `DEFAULT_CHUNKING_POLICY` | `kb-chunking-v1`, tokenizer `xlmr-d1287505`, identité persistée **`kb-chunking-v1+xlmr-d1287505`**, `max_tokens` 508 (512 − 2 tokens spéciaux − 2 du préfixe) |

Le compteur provisoire de S3 (`ApproximateTokenCounter`, identité `kb-chunking-v1+approx-xlmr-v1`) ne découpe plus en production ; il ne sert plus qu'aux tests de coût du `ChunkPlanner` sans modèle (en S5, il comptait aussi la question d'un passage à la recherche ; depuis la revue S5, c'est le tokenizer du profil, voir S5). Les lots extraits avec l'ancienne identité ne sont pas indexés (pas de backfill en S4).

**Modèle dans le worker** : `build_work_handlers(session_factory, qdrant_client, settings)` crée **un** `Embedder` paresseux par processus enfant ; l'extraction compte les tokens avec son tokenizer, l'indexation encode avec lui. Le processus maître ne le charge jamais ; l'API ne le charge pas en S4 ; depuis S5, elle le charge à la première recherche (ci-dessus). Premier démarrage : téléchargement dans `KB_MODELS_CACHE_DIR` (volume `models_cache`, ~1,1 Go de poids), à la première tâche du worker d'ingestion.

**Provisionnement** : `knowledge_base.provision_search_index(session_factory, qdrant_client)` (surface publique), lancé par `python -m runtime.provision`, service Compose `kb_provision` : après `migrate`, Qdrant sain ; l'API et les workers attendent sa fin. Choix S4 : une commande au démarrage de la stack plutôt qu'au `worker_process_init` (exécutée une fois, avant toute extraction, en échec visible si Qdrant ou PostgreSQL manquent ; jamais dans le chemin d'une tâche). Idempotente : relancée, elle ne crée rien. Si l'index actif a un autre profil ou une autre politique que la configuration, il reste actif et un avertissement est journalisé (migration blue/green : S9).

**Mesures** (2026-11-18 ; détail et conditions : [ADR 0008, suivi](../../../docs/decisions/0008-custom-pipeline-docling-extraction.md#s4--torch-et-sentencetransformers-2026-11-18), [ADR 0011, suivi](../../../docs/decisions/0011-embedding-profile-search-index-blue-green.md#suivi-2026-11-18)) :

| Mesure | Valeur |
|---|---|
| Image `production` (contenu) / `/app/.venv` | 152 → 479 Mo / 366 → 1 547 Mio (torch 687 Mio), aucun paquet `nvidia-*` |
| Chargement du modèle, cache chaud (conteneur Linux arm64) | 3,65 s ; premier démarrage avec téléchargement : 330 s dans `worker_ingestion` |
| RSS après chargement / après un lot de 32 (conteneur) | 904 Mio / 1 667 Mio ; `docker stats` du worker d'ingestion après la première publication : 1,03 Gio ; API : 192 Mio (torch non chargé) |
| Encodage, lot de 32 chunks de 152 tokens en moyenne | 91 ms/chunk (conteneur, 18 vCPU), 51 ms/chunk (hôte macOS arm64) ; tâche `index_version` de la fiche `qa_python_fastapi_fr.md` (3 chunks, modèle chargé) : 0,32 s |
| `ChunkPlanner`, tokenizer réel au lieu du compteur approximatif (hôte) | 2 Mo sans blanc : 0,21 s → 7,14 s, 1 345 → 802 chunks ; prose française de 200 Ko : 0,07 s → 0,33 s, 166 → 111 chunks |

Le tokenizer journalise une fois « Token indices sequence length is longer than … (806 > 512) » : la segmentation compte des unités entières, plus longues que le modèle ; seuls les chunks lui sont envoyés, tous sous 512 tokens préfixe compris.

**Variables** : `KB_MODELS_CACHE_DIR` (défaut `/var/cache/interview-rag-trainer/models`, fixé par Compose au point de montage de `models_cache`), `KB_EMBEDDING_BATCH_SIZE` (32). Hôte Qdrant : `QDRANT_*` du runtime.

Tables (migrations `448949d0e134` et `c43c7e850745` (revue S4), schéma `knowledge_base`) :

| Table | Contenu | Contraintes |
|---|---|---|
| `knowledge_corpus` | `corpus_id`, `active_index_id`, `version` (verrou optimiste) | clé primaire ; clé étrangère composite `(corpus_id, active_index_id) → search_indexes(corpus_id, index_id)`, `DEFERRABLE INITIALLY DEFERRED` (référence circulaire : contrôle au commit, le corpus est écrit avant son premier index) : l'index actif existe et appartient au corpus |
| `search_indexes` | `index_id`, `corpus_id`, `status`, `embedding_profile jsonb`, `chunking_policy jsonb`, `created_at`, `activated_at`, `retired_at` | `CHECK` du statut ; index unique partiel « un seul `ACTIVE` par corpus » (I10) ; `UNIQUE (corpus_id, index_id)`, cible de la clé étrangère du corpus |
| `indexations` | `(version_id, index_id)`, `status`, `chunking_policy_version`, `manifest_digest`, `point_count`, `completed_at`, `failure_*` | clés étrangères vers `document_versions` et `search_indexes` ; `COMPLETED` ⇔ digest, nombre et date renseignés ; `FAILED` ⇔ cause renseignée |

La même migration ajoute `UNIQUE (id, version_id, chunking_policy_version)` sur `knowledge_units` et la clé étrangère composite `document_chunks (unit_id, version_id, chunking_policy_version)` → ce `UNIQUE` (correction reportée de la revue S3 : un chunk ne peut référencer qu'une unité de son lot) ; la clé simple `unit_id` de S3 est conservée.

### S5 — recherche sémantique, provenance et évaluation

La query `SearchKnowledge` sert le contrat public `KnowledgeSearch` (aux autres contextes, par injection depuis `runtime`) et l'endpoint `POST /api/v1/knowledge-base/search`, qui passe par le même contrat ([knowledge-base.md §12, §14](../../../docs/conception/bounded-contexts/knowledge-base.md#12-stratégie-de-recherche-et-limites-de-recall), précisions S5 ; [ADR 0010 §5-6](../../../docs/decisions/0010-postgresql-source-of-truth-qdrant-derived-index.md)).

```text
index actif (SQL) ─► profil = celui de l'embedder ? ─► encodage « query: » (thread)
  ─► Qdrant groupé par unit_id, visible=true + filtres, limit × f unités
  ─► contrôle SQL : version PUBLISHED, document ACTIVE ─► complément si rejets
  ─► small-to-big (unité complète ou fenêtre ≤ max_passage_tokens) ─► tri par meilleur score
```

**Contrat public** (`contracts/`, bibliothèque standard seulement) : `KnowledgeSearch.search(KnowledgeQuery) -> KnowledgeSearchResult` (`contracts/providers/knowledge_search.py`), DTO `KnowledgeQuery`, `KnowledgeSearchResult`, `RetrievedPassage`, `Citation` (`contracts/result/`), champs du §14. Erreurs rendues au consommateur : `ValueError` (requête invalide : texte blanc ou de plus de `MAX_SEARCH_TEXT_LENGTH` = 2 000 caractères, `limit` hors de 1 à `MAX_SEARCH_LIMIT` = 50, type de document inconnu), `KnowledgeSearchUnavailableError(reason)` avec `UnavailabilityReason.NO_ACTIVE_SEARCH_INDEX` ou `SEARCH_PROFILE_MISMATCH`, erreurs PostgreSQL/Qdrant propagées (jamais un faux « rien trouvé »). Filtres : `topics` = **tous** requis, `kinds` et `document_ids` = **l'un** d'eux, `language` = langue du document.

**Domaine** (Python pur) :

| Élément | Rôle |
|---|---|
| `SearchFilters` (`domain/value_objects/search_filters.py`) | filtres de métadonnées ; refuse un `kind` inconnu |
| `ChunkHit`, `PublishedUnit`, `UnitChunk` (`domain/value_objects/`) | chunk trouvé (identifiants + score) ; unité publiée relue en SQL avec ses chunks dans l'ordre de lecture et sa citation (chemin et pages de l'unité) ; `PublishedUnit.blocks_token_count` = taille des blocs seuls (colonne `knowledge_units.token_count`) |
| `assemble_passage(unit, hits, *, max_tokens, token_counter)` (`domain/services/passage_assembler.py`) | small-to-big : unité complète (`Question : …` puis blocs) si `blocks_token_count` + tokens de la ligne `Question : …` (comptés par `token_counter`, en production le tokenizer du profil, déjà chargé par l'encodage de la requête) `≤ max_passage_tokens` ; sinon fenêtre contiguë autour du meilleur chunk, étendue vers un autre chunk trouvé, sinon vers la suite, sinon vers le début, tant que la somme des `token_count` des chunks tient ; un chunk seul plus grand que le budget est rendu seul ; `truncated=True` ; la question est en tête et l'en-tête contextuel de chaque chunk est retiré, entier ou tronqué par le `ChunkPlanner` (`strip_contextual_header` : préfixe de l'en-tête suivi de ` …`, ou `…` seul) ; un en-tête non reconnu laisse le texte du chunk entier |

Ports : `ChunkSearcher` (recherche groupée, `visible=true` toujours appliqué par l'implémentation), `ActiveIndexReader` (sans verrou), `PublishedUnitReader` (le contrôle SQL final).

**Use case** `use_cases/search_knowledge/` : `SearchKnowledge` (texte non blanc d'au plus 2 000 caractères, `1 ≤ limit ≤ 50`, filtres validés à la construction ; bornes `MAX_SEARCH_TEXT_LENGTH` et `MAX_SEARCH_LIMIT`, reprises par le schéma REST), `SearchKnowledgeHandler`, `SearchKnowledgeResult` / `FoundPassage` (types du domaine ; le cas d'usage n'importe jamais `contracts/`), `NoActiveSearchIndexError`, `SearchProfileMismatchError`. Règles (précisions S5 de la conception) :

- première passe : `unit_limit = limit × f` ; complément tant que des unités ont été écartées, qu'il manque des passages et qu'il reste des passes : `unit_limit = manquantes × f`, en excluant les unités écartées **et** déjà retenues ; arrêt dès qu'une passe rend moins d'unités que demandé ;
- `complete = false` si et seulement si des unités ont été écartées **et** qu'il reste moins de `limit` passages : un corpus simplement petit, sans rejet, est complet ;
- l'index actif doit avoir le profil de l'embedder (V1 : un seul embedder), sinon `SearchProfileMismatchError` avant tout encodage ;
- aucune session SQL n'est tenue pendant l'encodage : chaque lecture ouvre et ferme la sienne.

**Adapters** :

- `QdrantVectorIndex.search` : `query_points_groups(group_by="unit_id", limit=unit_limit, group_size=CHUNKS_PER_UNIT)` (3 : au plus trois chunks trouvés par unité, constante non mesurée, sans effet sur le jeu S5 où chaque unité tient en un chunk), filtre `search_filter` (`visible=true`, un `MatchValue` par sujet, `MatchAny` pour types et documents, `must_not` `MatchAny` sur `unit_id` exclus) ; payload lu : `chunk_id`, `unit_id`.
- `SqlAlchemyActiveIndexReader`, `SqlAlchemyPublishedUnitReader` (`adapters/gateway/persistence/`) : session courte par appel ; le contrôle SQL fait **deux requêtes** quel que soit le nombre d'unités (unités publiées par clé primaire avec jointures version et document, puis leurs chunks par l'index unique `(unit_id, ordinal_in_unit)`) ; il ne compte rien : la question est comptée par le domaine (`assemble_passage`). Revue S5 : la question était comptée ici par `ApproximateTokenCounter` (majorant, +3 à +16 tokens par question, médiane +10), règle déplacée dans le domaine et testée.
- `KnowledgeSearchAdapter` (`adapters/controller/contracts/`) : traduit le contrat vers le cas d'usage et retour, et les deux refus en `KnowledgeSearchUnavailableError`.
- REST `adapters/controller/rest/search_router.py` + `search_schemas.py` : `add_search_routes(router, *, knowledge_search)` reçoit une **fabrique** de `KnowledgeSearch`.

**REST** — `POST /api/v1/knowledge-base/search`, corps `{"text" (1 à 2 000 caractères, au moins un non blanc), "limit" (1 à 50, défaut 5), "language", "topics", "kinds", "document_ids"}` (champ inconnu refusé) :

| Code | Cas |
|---|---|
| 200 | `{"index_id", "complete", "passages": [{"unit_id", "unit_type", "question", "text", "truncated", "matched_chunk_ids", "score", "citation": {...}}]}` ; liste vide si rien ne correspond |
| 422 | corps invalide (texte blanc ou trop long, `limit` hors bornes, UUID illisible, `kind` inconnu, champ inconnu) |
| 503 | `{"code": "NO_ACTIVE_SEARCH_INDEX" \| "SEARCH_PROFILE_MISMATCH", "message"}` |
| 500 | PostgreSQL ou Qdrant injoignable |

**Modèle dans l'API** : `KnowledgeBaseResources` porte **un** `SentenceTransformersEmbedder` paresseux (`resources.embedder`, champ hors constructeur, cache `settings.models_cache_dir`), créé avec les ressources dans le `lifespan`, chargé à la première recherche dans un thread (jamais au démarrage : `/health/ready` n'en dépend pas). La route montée par `register` et la fabrique publique `build_knowledge_search(resources)` le reçoivent toutes deux : un consommateur composé par `runtime` sur les mêmes ressources ne charge pas le modèle une seconde fois (~1,1 Gio ; revue S5, I1). La recherche est reconstruite à chaque requête sur ce même embedder. Le tokenizer du profil compte aussi la question d'un passage (`embedder.token_counter`), sur la boucle asyncio, après l'encodage de la requête : un comptage concurrent d'un encodage dans un autre thread n'a produit ni erreur ni compte faux en 15 s d'essai sur un tokenizer rapide synthétique (`transformers` 5.23, `tokenizers` 0.23) ; non mesuré sur le tokenizer XLM-R réel. Volume `models_cache` monté dans `api` depuis S4.

**Réglages** (O12, [knowledge-base.md §16](../../../docs/conception/bounded-contexts/knowledge-base.md#16-points-ouverts)) : `KB_SEARCH_OVERSAMPLING_FACTOR` (2), `KB_SEARCH_COMPLEMENT_PASSES` (1), `KB_MAX_PASSAGE_TOKENS` (1024 ; refusé au démarrage sous `DEFAULT_CHUNKING_POLICY.max_tokens` = 508, un chunk entier de la politique de production).

**Évaluation** (marqueur `retrieval`, `tests/integration/knowledge_base/test_retrieval_evaluation.py`) : 42 requêtes FR/EN sur 10 documents (`tests/fixtures/knowledge_base/retrieval/`), pipeline de production (Docling, tokenizer réel, vrai E5). Le rapport JSON est écrit dans `KB_RETRIEVAL_REPORT_DIR` (défaut `backend/.cache/retrieval/retrieval_report.json`) ; les seuils de non-régression sont dans `retrieval_thresholds.json` (mesure moins deux requêtes du dénominateur, une pour l'inter-langues).

**Mesures** (2026-11-18) :

| Mesure | Valeur |
|---|---|
| Recall@1 / Recall@5 / MRR (sans filtre, 10 résultats) | 0,810 / 0,881 (37/42) / 0,853 |
| Intégrité d'unité (unité attendue dans les 5 premiers) | 1,000 (37/37), **non exercée** : aucune unité multi-chunks ni troncature dans le jeu ; à rendre significative en S6 |
| Recall@5 inter-langues / même langue | 0,750 (9/12) / 0,933 |
| Recall@5 avec filtres / sans filtre, mêmes requêtes | 0,975 (39/40) / 0,875 |
| Reproductibilité | deux exécutions : métriques et rangs identiques |
| Latence par composant, hôte macOS arm64 (médiane / p95, 126 recherches) | encodage 12,9 / 15,1 ms ; Qdrant 3,7 / 4,8 ms ; contrôle SQL 2,2 / 2,9 ms ; recherche complète 20,1 / 24,2 ms |
| Latence par composant, conteneur `api` de dev (médiane / p95, 50 recherches) | index actif 0,6 / 0,7 ms ; encodage 62,8 / 77,3 ms ; Qdrant 2,2 / 5,1 ms ; contrôle SQL 1,4 / 2,0 ms |
| Latence HTTP de bout en bout, `curl` depuis l'hôte vers la stack de dev (50 requêtes, modèle chargé) | médiane 69,2 ms, p95 105,3 ms |
| Premier appel (chargement du modèle compris) | 4,30 s (chargement journalisé : 1,9 s) |
| RSS du processus serveur de l'API | 146 Mio avant la première recherche → 1 267 Mio après, inchangé après 50 recherches |
| Taux de troncature (5 passages × 42 requêtes) | 0/210 pour des budgets de 508, 768, 1 024 et 1 536 : les 49 unités du jeu tiennent chacune dans un chunk (médiane 92 tokens, p90 178, max 427, question comprise) |
| Fenêtre de synchronisation (dev, un import) | item `sync_visibility` terminé 1,5 s après sa création ; le document était trouvé au premier sondage après le premier appel |

Simulation de rejets (O12) : 3 des 10 documents passés `WITHDRAWN` en SQL, leurs points encore visibles ; 24 requêtes dont le document attendu reste actif, `limit` 5 :

| f / passes | Recall@5 | MRR | résultats `complete=false` | latence médiane (hôte) |
|---|---|---|---|---|
| 1 / 0 | 0,875 | 0,812 | 66,7 % | 19,7 ms |
| 1 / 1 | 0,917 | 0,833 | 41,7 % | 24,6 ms |
| 1 / 2 | 0,917 | 0,833 | 29,2 % | 29,4 ms |
| 2 / 0 | 0,917 | 0,833 | 25,0 % | 21,6 ms |
| **2 / 1 (défaut)** | 0,917 | 0,833 | 12,5 % | 19,5 ms |
| 2 / 2 | 0,917 | 0,833 | 0 % | 19,6 ms |
| 3 / 0, 3 / 1, 3 / 2 | 0,917 | 0,833 | 0 % | 19,7 à 20,2 ms |

Limites du jeu, à lire avec ces chiffres (revue S5, M1) : 12 documents indexés, dont 10 visés par une requête et 2 distracteurs (Kubernetes, Redis) ; thèmes largement disjoints : hors des cinq documents Python (FastAPI, notes EN, asyncio, révision, pytest), un document par sujet, donc pas de négatif proche (deux documents qui répondent presque à la même question) ; 40 requêtes sur 42 ont des filtres, si bien que la mesure « avec filtres » couvre presque tout le jeu ; les 12 requêtes inter-langues sont pour la plupart des traductions ou reformulations de requêtes de même langue (x01/en01, x05/en08, y01/fr10, y02/fr12) ; aucune requête n'interroge un identifiant de code exact. Négatifs proches et requêtes à identifiants de code : S6/S9 ([feuille de route](../../../docs/conception/bounded-contexts/knowledge-base-roadmap.md)).

La latence de Qdrant ne dépend pas du facteur sur ce corpus (médiane 3,2 / 2,8 / 2,8 / 2,8 ms pour f = 1, 2, 4, 8). Concurrence : 320 encodages de requêtes lancés par lots de 64 en parallèle sur un même modèle (hôte) : aucune erreur, pas de verrou ajouté.

## Surface publique

`knowledge_base/__init__.py` réexporte, depuis `adapters/controller/dependencies.py` :

| Nom | Rôle |
|---|---|
| `register(app, resources)` | monte les routes d'import et de recherche sur l'application FastAPI, à sa construction ; `resources` est un fournisseur de `KnowledgeBaseResources`, appelé à chaque requête (les ressources sont créées par `runtime` dans le `lifespan`) ; réglages et modèle de la recherche viennent des ressources (une seule source depuis la revue S5) |
| `KnowledgeBaseResources(session_factory, settings, qdrant_client)` | ressources de l'API : fabrique de sessions du runtime, `KnowledgeBaseSettings` relu à chaque construction d'application, client Qdrant du processus (S5) ; portent aussi l'embedder paresseux de la recherche (`embedder`, hors constructeur, interne au contexte) |
| `build_knowledge_search(resources)` | implémentation du contrat public `knowledge_base.contracts.providers.KnowledgeSearch` (S5) sur l'embedder des ressources, partagé avec la route ; ne se connecte à rien, ne charge pas le modèle. Variante interne pour les tests : `adapters/controller/composition.compose_knowledge_search(session_factory, qdrant_client, settings, *, embedder, token_counter)` |
| `register_worker(celery_app, resources, settings=None)` | déclare les tâches `knowledge_base.work.{execute,dispatch,recover}`, leurs routes et le `beat_schedule`, dans le processus maître ; `resources` est un fournisseur appelé seulement à l'exécution d'une tâche |
| `KnowledgeBaseWorkerResources(session_factory, loop, handlers=())` | ressources d'un processus enfant : fabrique de sessions, boucle asyncio persistante, handlers (registre construit à la création) |
| `KnowledgeBaseSettings` | réglages `KB_*` (pydantic-settings) |
| `WorkHandler` | Protocol d'un handler : `kind`, `queue`, `max_attempts` (appliqué au claim), `execute(item, ctx)` ; hook facultatif `on_terminal_failure(session, item, error)` |
| `build_work_handlers(session_factory, qdrant_client, settings=None)` | handlers de production des work items (`extract_version`, `index_version`, `sync_visibility`), construits par `runtime` dans chaque processus enfant sur sa fabrique de sessions et son client Qdrant ; ne se connecte à rien et ne charge pas le modèle (chargé à la première tâche) |
| `provision_search_index(session_factory, qdrant_client)` | crée et active l'index initial s'il n'existe pas (idempotent) ; rend l'index actif ; appelé par `python -m runtime.provision` au démarrage de la stack |

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
  use_cases/import_document/              # ImportDocument, ImportDocumentHandler (S2)
  use_cases/import_document_version/      # ImportDocumentVersion, ImportDocumentVersionHandler (S2)
  use_cases/import_steps.py               # nom de fichier, liste blanche, empreinte de requête, idempotence, enqueue / cancel, rejeu après course
  use_cases/import_result.py              # ImportDocumentResult, commun aux deux imports (S2)
  use_cases/errors.py                     # InvalidFilename, UnsupportedDocumentType, DocumentNotFound, DuplicateContentOtherDocument, IdempotencyKeyConflict
  use_cases/record_extraction/            # RecordExtraction, RecordExtractionHandler (prepare, apply, fail), PreparedExtraction (S3) ; indexations (S4)
  use_cases/indexing_steps.py             # kinds index_version / sync_visibility, clés de dédoublonnage, enqueue (S4)
  use_cases/complete_indexation/          # CompleteIndexation, CompleteIndexationHandler (prepare, apply, cancel_if_obsolete, fail), erreurs (S4)
  use_cases/publish_document_version/     # PublishDocumentVersion, PublishDocumentVersionHandler (apply), refus (S4)
  use_cases/sync_document_visibility/     # SyncDocumentVisibility, SyncDocumentVisibilityHandler (S4)
  use_cases/provision_search_index/       # ProvisionSearchIndex, ProvisionSearchIndexHandler (S4)
  domain/entities/knowledge_corpus.py     # KnowledgeCorpus, SearchIndex, IndexStatus, CORPUS_ID (S4)
  domain/entities/indexation.py           # Indexation, IndexationStatus (S4)
  domain/value_objects/                   # S4 : embedding_profile, index_manifest, indexed_point
  domain/services/point_id.py             # uuid5(index_id, chunk_id) (S4)
  domain/exceptions/                      # S4 : corpus_errors, indexation_errors
  domain/providers/                       # S4 : embedder, vector_index, chunk_reader, knowledge_corpus_repository, indexation_repository, indexing_transaction
  adapters/failure_policy.py              # FailureDecision, decide_recovery — purs, partagés controller / gateway
  adapters/controller/dependencies.py     # register, register_worker, build_work_handlers, provision_search_index et réexports de la surface publique
  adapters/controller/composition.py      # build_provision_search_index_handler (S4)
  adapters/controller/rest/               # routes d'import, schémas HTTP, traduction des refus, KnowledgeBaseResources (S2)
  adapters/controller/tasks/
    execution.py                          # execute_work_item, WorkContext (heartbeat, finalize)
    dispatch.py                           # dispatch_pending, recover_expired_leases
    registry.py                           # WorkHandler, HandlerRegistry
    resources.py                          # KnowledgeBaseWorkerResources
    retry_policy.py                       # backoff (equal jitter), classification des erreurs — purs
    extract_version_handler.py            # ExtractVersionWorkHandler, build_extract_version_handler (S3)
    index_version_handler.py              # IndexVersionWorkHandler, build_index_version_handler (S4)
    sync_visibility_handler.py            # SyncVisibilityWorkHandler, build_sync_visibility_handler (S4)
    lease.py                              # keeping_lease : heartbeats pendant un travail long, annulé au premier refus (S3, partagé en S4)
  adapters/gateway/persistence/
    sqlalchemy_work_queue.py              # SqlAlchemyWorkQueue (port), SqlAlchemyWorkLifecycle (transitions §3)
    sqlalchemy_unit_of_work.py            # SqlAlchemyUnitOfWork (S2)
    sqlalchemy_knowledge_document_repository.py, sqlalchemy_idempotency_key_repository.py
    constraint_violations.py              # nom de la contrainte violée (traduction des IntegrityError attendues)
    sqlalchemy_extraction_repository.py, sqlalchemy_extraction_transaction.py  # lot d'unités et chunks (S3)
    sqlalchemy_knowledge_corpus_repository.py, sqlalchemy_indexation_repository.py, sqlalchemy_chunk_reader.py, sqlalchemy_indexing_transaction.py  # S4
  adapters/gateway/blob/filesystem_blob_store.py  # FileSystemBlobStore : put (S2), read (S3)
  adapters/gateway/parsing/               # RoutingDocumentParser, DoclingMarkdownParser, PlainTextParser (S3)
  adapters/gateway/embedding/approximate_token_counter.py  # TokenCounter provisoire (S3) ; hors production depuis S4 (tests de coût)
  adapters/gateway/embedding/sentence_transformers_embedder.py  # SentenceTransformersEmbedder : préfixes, normalisation, tokenizer (S4)
  adapters/gateway/vector_store/qdrant_vector_index.py  # QdrantVectorIndex : collection par index, payload, manifest (S4)
  contracts/providers/knowledge_search.py # KnowledgeSearch, KnowledgeSearchUnavailableError, UnavailabilityReason (S5)
  contracts/result/                       # KnowledgeQuery, KnowledgeSearchResult, RetrievedPassage, Citation (S5)
  domain/value_objects/                   # S5 : search_filters, chunk_hit, published_unit
  domain/services/passage_assembler.py    # reconstruction small-to-big (S5)
  domain/providers/                       # S5 : chunk_searcher, active_index_reader, published_unit_reader
  use_cases/search_knowledge/             # SearchKnowledge, SearchKnowledgeHandler, SearchKnowledgeResult, erreurs (S5)
  adapters/controller/contracts/          # KnowledgeSearchAdapter (S5)
  adapters/controller/rest/search_router.py, search_schemas.py  # POST /search (S5)
  adapters/gateway/persistence/sqlalchemy_active_index_reader.py, sqlalchemy_published_unit_reader.py  # S5
  frameworks/sqlalchemy/tables.py         # MetaData(schema="knowledge_base"), work_items, documents, document_versions, idempotency_keys, knowledge_units, document_chunks, knowledge_corpus, search_indexes, indexations
  frameworks/configuration/chunking.py    # DEFAULT_CHUNKING_POLICY (tokenizer du profil depuis S4)
  frameworks/configuration/settings.py    # KnowledgeBaseSettings
```

## Écrire un handler (slices S2 et suivantes)

1. Effets externes **idempotents**, hors transaction ; `await ctx.heartbeat()` au moins toutes les `ctx.heartbeat_interval` pour un traitement long, de préférence par `keeping_lease(ctx, travail(), intervalle)` qui annule le travail dès qu'un heartbeat est refusé (aucun effet externe d'une tentative périmée après son heartbeat suivant ; l'appel en cours, lui, peut aboutir) ; travail CPU dans `asyncio.to_thread`.
2. `await ctx.finalize(callback)` : `callback(session)` écrit l'état métier ; le passage `SUCCEEDED` est ajouté dans la même transaction, conditionné au fencing token. Un token périmé annule toute la transaction (`StaleAttempt`).
3. Erreurs : `TerminalWorkError(code, message)` → `FAILED` ; `TransientWorkError` et toute autre exception → nouvelle tentative avec backoff tant que `attempts < max_attempts`. Un handler qui se termine sans `ctx.finalize` est un échec terminal `NOT_FINALIZED` (le hook est appelé).
4. Aucune écriture PostgreSQL métier hors de `ctx.finalize` (ou du hook `on_terminal_failure`, exécuté dans la transaction de `fail`).
5. Si `on_terminal_failure` lève : la transaction de `fail` est annulée (aucune écriture du hook), puis nouvelle tentative avec backoff et le code `TERMINAL_HOOK_FAILED` ; à la dernière tentative, `FAILED` (`TERMINAL_HOOK_FAILED`) **sans** hook. L'état métier de ce cas, comme celui d'un `FAILED` par `recover`, est le point ouvert O14 ([knowledge-base.md §16](../../../docs/conception/bounded-contexts/knowledge-base.md#16-points-ouverts)).

`max_attempts` : `enqueue(max_attempts=None)` applique le défaut de la colonne (5) ; le **claim** remplace cette valeur par celle du handler du `kind`, dans le même `UPDATE`. `retry_later` et `recover` lisent la colonne.

En S0, `finalize` et `on_terminal_failure` reçoivent une `AsyncSession` ; l'Unit of Work du contexte (`SqlAlchemyUnitOfWork`) existe depuis S2, côté API.

`FailureInfo` et `FailureReason` restent distincts (pas la même forme) :

- `FailureInfo` (`domain/providers/work_queue.py`, code stable + message tronqué à 2 000 caractères) décrit l'échec d'une **tentative** de work item : colonnes `last_error_*` et argument du hook `on_terminal_failure` (S0 §5) ;
- `FailureReason` (`domain/value_objects/failure_reason.py`, `code`, `message`, `retryable`) est l'état **durable** d'une version `FAILED` ; `retryable` dit si `RetryIngestion` a un sens.

Le handler `extract_version` traduit l'un en l'autre dans son hook (`retryable` faux pour tout code de `ParseErrorCode` — `EMPTY_DOCUMENT`, `INVALID_DOCUMENT`, `DOCUMENT_TOO_COMPLEX` — et pour `CHUNKING_FAILED`) ; une fusion ne sera envisagée que si les deux formes convergent.

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
- Intégration (`make test-integration`) : `backend/tests/integration/knowledge_base/`, scénarios 1 à 14 de S0 §12.2 contre PostgreSQL et RabbitMQ réels ; worker in-process (pool solo) et vrai worker prefork en sous-processus (scénarios 13 et 14). S2 : `test_import_api` (POST multipart réel → lignes, blob, work item), `test_import_atomicity` (orphelin après rollback, verrou optimiste, course sur un même nouveau fichier avec barrière explicite), `test_import_persistence`, `test_import_migration` ; le volume de blobs est un répertoire temporaire (`KB_BLOB_ROOT`). S3 : `test_extraction_flow` (import réel → `EXTRACTED`, unités, chunks, citations, Docling réel), `test_extraction_fencing`, `test_extraction_failures`, `test_extraction_rerun` (mêmes identifiants après un crash), `test_extraction_blob_missing`, `test_extraction_migration`. S4 (PostgreSQL et Qdrant réels ; chaque scénario tourne avec l'`Embedder` de test `hash` et avec le vrai modèle, paramètre marqué `model`) : `test_indexing_flow` (import → `PUBLISHED`, payload, invisibilité avant synchronisation), `test_indexing_idempotence` (panne entre upserts et finalisation, rejeux, zombie), `test_indexing_integrity` (point perdu ou modifié, collection non conforme ou absente), `test_indexing_visibility`, `test_qdrant_vector_index` (contrat de l'adapter), `test_indexing_migration`. Revue S4 : `test_knowledge_corpus_repository` (`FOR SHARE` contre `FOR UPDATE NOWAIT`, provisionnement concurrent réel, verrou optimiste de `save`), `test_corpus_active_index_migration` (clé étrangère de l'index actif, aller-retour ancré sur `c43c7e850745`).
- Unitaires S4 : `domain/test_{embedding_profile,index_manifest,indexation,knowledge_corpus}.py`, `use_cases/test_{record_extraction_indexations,complete_indexation,publish_document_version,sync_document_visibility,provision_search_index}.py` (doubles `indexing_fakes.py` : `HashEmbedder`, Qdrant en mémoire, garde « aucun appel externe dans une transaction »), `test_indexing_work_handlers.py`, `test_index_version_lease.py` (heartbeats pendant un `prepare` lent, heartbeat refusé → `prepare` interrompu sans upsert ni finalisation, intervalle de la composition de production), `test_lease.py` (`keeping_lease` : annulation au refus, aucune tâche survivante, annulation de l'appelant), `adapters/embedding/` (préfixes, normalisation, tokenizer, sans modèle), `adapters/vector_store/` ; `architecture/test_embedding_confinement.py`.
- Unitaires S5 : `use_cases/test_search_knowledge.py` (contrôle SQL, complément, regroupement, score, provenance), `use_cases/test_search_knowledge_passages.py` (small-to-big), `use_cases/test_search_profile_and_contract.py` (profil de l'index, traduction et erreurs du contrat), `domain/services/test_passage_assembler.py` (chunks du vrai `ChunkPlanner`), `adapters/vector_store/test_search_filter.py`, `test_search_routes_http.py`, `test_search_routes_errors.py`, `test_build_knowledge_search.py`, `test_search_settings.py`, `test_retrieval_metrics.py`, `test_retrieval_reference_set.py` ; `architecture/test_knowledge_search_contract.py`. Doubles : `use_cases/search_fakes.py`.
- Intégration S5 : `test_search_flow` (bout en bout, vrai E5 : citation complète, filtres, version non publiée, non synchronisée, supplantée, document retiré, `complete=false`, troncature, REST), `test_qdrant_chunk_search` (groupement, exclusion, `visible`, filtres sur Qdrant réel), `test_retrieval_evaluation` (marqueurs `model` et `retrieval`).
- Tests `model` : cache des poids `SENTENCE_TRANSFORMERS_HOME` (`make test-integration` le pointe vers `backend/.cache/models`, ignoré par git) ; premier lancement : téléchargement d'environ 1,1 Go.
