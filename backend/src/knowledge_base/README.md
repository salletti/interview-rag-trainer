# Knowledge Base

Bounded Context ([ADR 0007](../../../docs/decisions/0007-knowledge-base-bounded-context.md)) chargé des documents de connaissances, de leur ingestion (extraction, chunking, embeddings) et de la recherche sémantique publiée vers les autres contextes. Conception : [knowledge-base.md](../../../docs/conception/bounded-contexts/knowledge-base.md) ; feuille de route : [S0–S9](../../../docs/conception/bounded-contexts/knowledge-base-roadmap.md).

## État : slices S0 à S9

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
| 413 | `PAYLOAD_TOO_LARGE` | `max_bytes` | fichier au-delà de `KB_MAX_UPLOAD_BYTES`, ou `Content-Length` déclaré au-delà de `KB_MAX_UPLOAD_BYTES` + 64 Kio (refus anticipé, corps non lu) ; ni blob ni ligne |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | `filename`, `media_type` | couple type / extension hors liste blanche |
| 404 | `DOCUMENT_NOT_FOUND` | `document_id` | document cible inconnu |
| 409 | `DUPLICATE_CONTENT` | `document_id`, `version_id` (version existante) | contenu déjà présent dans ce document (I3) ou dans un autre (O2) |
| 409 | `IDEMPOTENCY_KEY_CONFLICT` | — | même clé, autre requête |
| 409 | `DOCUMENT_WITHDRAWN` | `document_id` | document retiré (nouvelle version ou réimport de son contenu) |
| 409 | `CONCURRENT_MODIFICATION` | — | course perdue deux fois de suite (la première est rejouée par le handler) ; le client rejoue |
| 422 | format FastAPI (`detail`) | — | formulaire invalide (champ manquant, `kind` inconnu, identifiant non UUID) |

Le fichier n'est jamais lu en entier en mémoire : Starlette stocke la partie multipart dans un fichier temporaire (en mémoire jusqu'à 1 Mio, sur disque au-delà) **avant** l'appel de la route, puis le routeur la transmet au blob store par morceaux de 64 Kio. Pic mesuré (`tracemalloc`) pour un dépôt de 20 Mio : environ 120 Kio. La limite exacte ne s'applique donc qu'après la réception complète par Starlette.

**Refus anticipé sur `Content-Length`** (revue S2, M-1) : les deux routes d'import refusent en 413 `PAYLOAD_TOO_LARGE`, **sans lire le corps**, une requête dont le `Content-Length` déclare plus que `KB_MAX_UPLOAD_BYTES` + `MULTIPART_ALLOWANCE_BYTES` (64 Kio). La marge couvre l'enveloppe multipart (frontières, en-têtes des parties, titre, sujets), que l'en-tête compte en plus du fichier : sans elle, un fichier à la limite serait refusé d'avance ; un formulaire de plus de 64 Kio de métadonnées avec un fichier proche de la limite l'est. FastAPI lisant le formulaire avant toute dépendance, le contrôle est porté par la classe de route (`documents_router._declared_length_limited_route`) ; la limite est lue à chaque requête dans les réglages des ressources. Un en-tête absent (`Transfer-Encoding: chunked`), illisible ou mensonger laisse la décision au contrôle au fil du flux, qui reste la seule borne exacte. Borner le corps HTTP lui-même (sans `Content-Length`) relève toujours du serveur ou du proxy.

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

**Parseurs** (`adapters/gateway/parsing/`, port `DocumentParser`, synchrone) : `RoutingDocumentParser` aiguille sur le type de média (sans paramètre ni casse) ; `text/markdown` → `DoclingMarkdownParser`, `text/plain` → `PlainTextParser` (adapter natif, point O7). Docling n'est importé que par les modules `docling_*.py` de `parsing/` (test d'architecture `test_docling_confinement` ; depuis S6, mapping, parseurs Markdown, PDF et DOCX, pypdfium2 compris), et seulement à la première conversion : l'API compose le handler sans charger Docling.

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

Depuis S8, la classification de tous les codes vient du catalogue unique (`domain/value_objects/error_catalog.py`, [§15.5](../../../docs/conception/bounded-contexts/knowledge-base.md#155-catalogue-des-codes-derreur-s8)) ; `failure_retryable` = « rejouable » du catalogue.

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

- `RecordExtraction.apply` (S3, étendu) : dans la transaction qui passe la version `EXTRACTED`, lit le corpus sous **`FOR SHARE`** et crée, pour chaque index `ACTIVE` ou `BUILDING` dont la politique est celle du lot, une `Indexation PENDING` et un item `index_version` (dédoublonnage `index_version:<version_id>:<index_id>`). Sans corpus : aucune indexation (la version reste `EXTRACTED`). Index d'une autre politique : ignoré avec un avertissement en S4 ; *depuis S9, indexation `PENDING` de la politique de l'index et item `rechunk_version`*. Une version passée `EXTRACTED` sans **aucune** indexation est journalisée en `WARNING` avec le code `EXTRACTED_WITHOUT_INDEXATION` (constante de `record_extraction_handler.py`, dans le message `code=… document_id=… version_id=…` et en attribut `code` de l'enregistrement) : repère de la réconciliation S8.
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

### S6 — extraction PDF/DOCX avec Docling

PDF natifs et DOCX rejoignent la liste blanche d'import et le pipeline d'extraction de S3, sans autre changement du domaine ni du schéma ([roadmap S6](../../../docs/conception/bounded-contexts/knowledge-base-roadmap.md#s6--extraction-pdfdocx-avec-docling), [knowledge-base.md, précisions S6](../../../docs/conception/bounded-contexts/knowledge-base.md#151-composants), [ADR 0008](../../../docs/decisions/0008-custom-pipeline-docling-extraction.md), [ADR 0012, suivi S6](../../../docs/decisions/0012-original-document-storage-and-purge.md)).

**Import** (`use_cases/import_steps.py`) : `application/pdf` avec `.pdf`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document` avec `.docx` ; le type doit concorder avec l'extension, comme pour Markdown et TXT ; le Word binaire historique (`application/msword`, `.doc`) reste refusé (415). Aucune limite de pages à l'import : la limite de 300 pages s'applique au parsing (ci-dessous) ; la limite d'octets (`KB_MAX_UPLOAD_BYTES`, 20 Mio) reste la seule limite HTTP.

**Routage** (`default_parser()` d'`extract_version_handler.py`) : par type de média (`Document.source.media_type`), jamais par `DocumentKind` ni par extension : `application/pdf` → `DoclingPdfParser`, DOCX → `DoclingDocxParser`, en plus de Markdown et TXT. Le type DOCX est une seule constante (`DOCX_MEDIA_TYPE` d'`import_steps`, que l'adapter importe) ; le test de contrat `test_every_importable_media_type_has_a_parser` vérifie que chaque type de la liste blanche d'import (`SUPPORTED_TYPES`) a son parseur.

**Traduction commune** (`adapters/gateway/parsing/docling_mapping.py`, `structured_document_from_docling`) : la couche anti-corruption de S3, sortie du parseur Markdown pour servir aussi PDF et DOCX (le parseur Markdown l'appelle ; les golden tests de S3 n'ont pas bougé). Parcours en ordre de lecture de **tout** l'arbre (Docling imbrique le contenu d'un DOCX sous le titre qui le précède) ; `TitleItem` → titre 1, `SectionHeaderItem(n)` → titre `min(n + 1, 6)` ; code, tableaux, listes, paragraphes à mise en forme comme en S3 ; le parcours ne descend **pas** dans un tableau : ses enfants sont le contenu de ses cellules mises en forme (groupes `rich_cell_group_*` de Docling, Markdown et DOCX), déjà dans les lignes du `Table` (revue S6 : il ressortait en `Paragraph` en double) ; ses légendes puis ses notes (PDF) sont émises en `Paragraph` après lui ; une cellule de plusieurs lignes (DOCX à plusieurs paragraphes) est jointe par des espaces, une rangée par ligne ; `page` = première page de provenance (pour une liste, celle de son premier élément), `None` sans provenance ; couche `furniture` (en-têtes et pieds de page) et images ignorées. Un PDF n'a aucune hiérarchie de titres (Docling met tout au niveau 1) : tous ses titres sont de niveau 2 et le chemin de section d'une unité PDF se réduit à son dernier titre. Le code d'un PDF perd retours à la ligne et indentation (limite de Docling).

**PDF** (`docling_pdf_parser.py`, `DoclingPdfParser(*, max_pages=MAX_PDF_PAGES)`, `MAX_PDF_PAGES = 300`) :

1. avant le pipeline de layout, ouverture par pypdfium2 (moteur PDF de Docling, sous son verrou `pypdfium2_lock`) : illisible, chiffré ou corrompu → `INVALID_DOCUMENT` (message avec le nom du fichier) ; plus de `max_pages` pages → `TOO_MANY_PAGES` (nombre de pages et limite dans le message) ; aucune page avec un caractère visible dans sa couche texte → `OCR_REQUIRED`. Quelques millisecondes pour 300 pages, sans modèle ;
2. PDF partiellement scanné : extrait, ses pages sans texte sont ignorées et leur nombre journalisé (avertissement) ;
3. pipeline PDF de Docling (`pdf_pipeline_options()`) : OCR désactivé, structure des tableaux (TableFormer) activée, CPU explicite, modèle de layout `docling-layout-heron` à la révision épinglée `LAYOUT_MODEL_REVISION` (Docling suit `main`), une page à la fois par modèle et quatre pages au plus en attente entre deux étapes (mesures ci-dessous) ;
4. conversion impossible (`ConversionError`) → `INVALID_DOCUMENT`, message avec l'erreur de Docling et sa cause première ; conversion partielle (`PARTIAL_SUCCESS`) acceptée, statut et erreurs journalisés (avertissement) ; aucun bloc → `EMPTY_DOCUMENT`. Toute autre erreur (téléchargement des poids, par exemple) reste transitoire : retry avec backoff.

**DOCX** (`docling_docx_parser.py`, `DoclingDocxParser(*, max_xml_bytes=MAX_DOCX_XML_BYTES)`) : backend Word de Docling, sans modèle ; le style « Titre » donne le niveau 1, « Titre n » le niveau n + 1 ; pas de pagination (`page = None`), donc **pas de limite de pages**. Avant Docling, la somme des tailles décompressées **déclarées** (`ZipInfo.file_size`) des parties `word/*.xml` est bornée à `MAX_DOCX_XML_BYTES` = 100 Mio (revue S6, O16) : au-delà, `DOCUMENT_TOO_COMPLEX` (taille et limite dans le message) ; une bombe de décompression de ~100 Kio passe la limite d'octets de l'import, pas celle-ci. La taille déclarée suffit : Docling lit l'archive par python-docx, donc par `zipfile`, qui tronque une partie à sa taille déclarée. ZIP illisible (vide, texte, tronqué, document chiffré qui est un conteneur OLE) → `INVALID_DOCUMENT` avant Docling ; ZIP sans document Word ou XML refusé par Docling → `INVALID_DOCUMENT`, message neutre (« illisible ou corrompu », ou « structure invalide ou trop complexe » si la cause est une erreur de syntaxe XML, dont la profondeur maximale de 256 de lxml) avec la cause première.

**Chargement** : Docling, pypdfium2 et les modèles sont importés à la première conversion, pas à la composition : l'API compose le handler d'extraction sans eux (vérifié : aucun de `docling`, `pypdfium2`, `cv2`, `torchvision` dans `sys.modules` après `default_parser()`). Chaque processus enfant du worker crée ses convertisseurs à sa première conversion. Le travail CPU tourne dans le thread de `RecordExtractionHandler.prepare` (`asyncio.to_thread`) pendant que la boucle envoie les heartbeats (`keeping_lease`, test `test_heartbeats_are_sent_while_the_pdf_is_being_extracted`).

**Conversion zombie et verrou** (`docling_conversion.py`, `DoclingConversion`, commun aux trois parseurs Docling) : quand `keeping_lease` abandonne une tentative (heartbeat refusé, lease perdu), il annule la coroutine, mais le thread `to_thread` qui convertit va à son terme ; le worker prend aussitôt la tâche suivante. Un `threading.Lock` par parseur couvre la création paresseuse du convertisseur et chaque conversion : la conversion suivante du même parseur **attend** la fin du thread zombie (heartbeats envoyés pendant l'attente) au lieu de partager avec lui un `DocumentConverter` que Docling ne garantit pas réentrant, ou d'en charger un second. Les conversions d'un parseur sont donc sérialisées dans un processus ; un PDF et un DOCX (parseurs distincts) peuvent encore se chevaucher. La durée « converties par Docling en … s » journalisée inclut l'attente du verrou. Tests : `test_docling_conversion.py`.

**Poids des modèles** : `docling-layout-heron` (Apache-2.0, 164 Mio) et `docling-models` v2.3.0 (TableFormer, CDLA-Permissive-2.0, 342 Mio ; Docling télécharge les variantes `fast` et `accurate`), téléchargés au premier PDF dans le cache du Hub : `HF_HOME` = `models_cache/huggingface` sous Compose (volume partagé avec le modèle d'embeddings), `backend/.cache/huggingface` pour `make test-integration`.

**Polices de l'image** : docling-parse dessine lui-même les pages que lit le modèle de layout. Sans police système (image `python:3.13-slim`), les polices standard non embarquées (Helvetica, Courier…) sont rendues en rectangles : le modèle ne reconnaissait plus ni titres ni code (0 bloc de code sur les fixtures, confiance 0,6 à 0,8 au lieu de 0,94 à 0,99). L'image installe `fonts-urw-base35` (clones des 35 polices PostScript standard) et `fonts-dejavu-core` (repli) : sorties identiques à l'hôte sur les quatre PDF de fixtures (Liberation, essayée, classait encore un bloc de code en paragraphe). Les tests d'intégration tournent sur l'hôte : un écart de rendu propre au conteneur ne serait pas vu par eux. D'où le test de fumée `make smoke-pdf-image` (`backend/scripts/smoke_pdf_parser.py`) : dans un conteneur éphémère de l'image de `worker_ingestion`, il convertit `cours_git_fr.pdf` et échoue s'il n'en sort pas au moins un `Heading` et un `CodeBlock` (vérifié : polices masquées, 0 `CodeBlock`, code 1).

**Codes d'erreur ajoutés** (`ParseErrorCode`, donc terminaux ; `failure_retryable` faux, sauf `TOO_MANY_PAGES` depuis la revue S8 : rejouable une fois la limite de pages relevée) :

| Code | Cause |
|---|---|
| `OCR_REQUIRED` | PDF sans aucune couche texte (scan) ; l'OCR est désactivé en V1 (ADR 0008, point 5) |
| `TOO_MANY_PAGES` | PDF de plus de 300 pages (ADR 0012, point 4) |
| `INVALID_DOCUMENT` (élargi) | PDF chiffré, corrompu ou illisible ; DOCX illisible, chiffré ou refusé par Docling |
| `DOCUMENT_TOO_COMPLEX` (élargi) | DOCX dont les parties `word/*.xml` déclarent plus de 100 Mio décompressés (revue S6, O16) |

**Mesures** : voir [ADR 0012, suivi S6](../../../docs/decisions/0012-original-document-storage-and-purge.md) (CPU, RSS, durée par page ; limites confirmées) et [ADR 0008, suivi S6](../../../docs/decisions/0008-custom-pipeline-docling-extraction.md) (paquets, image). Évaluation : [ci-dessous](#évaluation-s6).

#### Évaluation S6

- `test_retrieval_evaluation_office` : le jeu de S5 rejoué à l'identique, `qa_pytest_fr` importé en PDF et `cours_git_fr` en DOCX ; seuils de S5 inchangés.
- `test_retrieval_long_units` (jeu propre à S6, `fixtures/knowledge_base/office/long_units_reference.json`) : le cours PDF `cours_concurrence_long_fr.pdf`, dont une section de ~1 300 tokens (pages 1 et 2) fait plusieurs chunks et dépasse `max_passage_tokens`, au milieu de quatre documents Python voisins (négatifs proches) ; 10 requêtes dont 4 à identifiant de code exact et une inter-langues ; rejouées pour des budgets de passage de 508 à 2 048 tokens.

**Mesures** (2026-11-18, hôte macOS arm64, vrai E5 et vrai Docling ; deux exécutions identiques) :

| Jeu | Recall@5 | MRR | Intégrité | Autres |
|---|---|---|---|---|
| S5 (Markdown), référence | 0,881 (37/42) | 0,853 | 1,000 (37/37) | inter-langues 0,750 ; avec filtres 0,975 |
| S5 avec `qa_pytest_fr` en PDF et `cours_git_fr` en DOCX | 0,905 (38/42) | 0,857 | 1,000 (38/38) | inter-langues 0,750 ; avec filtres 1,000 ; 8 requêtes sur les documents convertis : Recall@5 au-dessus du seuil de S5 (test), citations PDF avec pages 1 à 2, DOCX sans page |
| Unités longues, budget 1 024 (défaut) | 0,900 (9/10) | 0,833 | **0,875 (7/8) sur passages tronqués** | section longue dans les 5 premiers pour 8/8 requêtes, tronquée dans les 8 cas |

Aucune régression des seuils de S5. Sur le jeu des unités longues, l'unité est reconnue à sa **provenance** (fichier et section) : la définition de S5, qui la reconnaît à l'extrait de réponse attendu, ne voit pas une fenêtre qui a perdu la réponse (l03 : comptée « non retrouvée », intégrité de S5 1,000) ; la provenance la compte comme un défaut d'intégrité (0,875). Seuils propres à ce jeu : `office/long_units_thresholds.json` (mesure moins une requête).

Budget de passage (O12, même jeu) :

| `max_passage_tokens` | Passages longs tronqués | Intégrité des tronqués | Recall@5 / MRR | Tokens du passage attendu (médiane / max) | Tokens des 5 premiers passages (médiane / max) |
|---|---|---|---|---|---|
| 508 et 768 (un chunk) | 8/8 | 0,625 (5/8) | 0,900 / 0,833 | 451 / 487 | 1 210 / 1 285 |
| **1 024** (deux chunks) | 8/8 | 0,875 (7/8) | 0,900 / 0,833 | 843 / 938 | 1 661 / 1 772 |
| 1 536 et 2 048 | 0/8 (unité entière : 1 294) | — | 1,000 / 0,883 | 1 294 / 1 294 | 2 052 / 2 128 |

Le défaut restant à 1 024 (l03) : la fenêtre garde les deux chunks trouvés, la réponse est dans le troisième. 1 536 corrige ce cas parce que l'unité mesure 1 294 tokens : résultat propre à cette fixture, qui ne suffit pas à trancher O12 (voir [knowledge-base.md §16](../../../docs/conception/bounded-contexts/knowledge-base.md#16-points-ouverts)).

### S7 — versions concurrentes, retrait et purge

Une version obsolète n'est jamais publiée ni visible, un retrait est immédiat pour toute recherche commencée après son commit, puis les données du document sont purgées ([roadmap S7](../../../docs/conception/bounded-contexts/knowledge-base-roadmap.md#s7--versions-concurrentes-retrait-et-purge), [knowledge-base.md §8, §10, précisions S7](../../../docs/conception/bounded-contexts/knowledge-base.md#10-publication-retrait-et-purge), [ADR 0012, suivi S7](../../../docs/decisions/0012-original-document-storage-and-purge.md)).

```text
POST /documents/{id}/versions ──► N+1 RECEIVED ; N DISCARDED, ses items extract_version ET index_version annulés,
                                  ses indexations PENDING/FAILED → CANCELLED (une transaction)
DELETE /documents/{id}        ──► WITHDRAWN, visibility_revision+1, travail en cours annulé, item purge_document (202)
purge_document                ──► points Qdrant (index vivants)                    (hors transaction)
                              ──► chunks, unités, documents.purged_at              (finalisation sous fencing)
                              ──► blobs non détenus                                 (après le commit, au mieux)
```

**Import de N+1** (`ImportDocumentVersionHandler`, report de S4) : pour chaque version écartée par cet import (événements `DocumentVersionDiscarded`) et chaque index vivant (`corpus.get_shared().live_indexes()`, lu seulement s'il y a une version écartée), l'item `index_version` est annulé (fencing : la tentative en cours de N ne finalise plus, `stale`) et l'indexation `PENDING` ou `FAILED` passe `CANCELLED` ; une `COMPLETED` reste telle quelle (aucune transition). Étape commune avec le retrait : `cancel_indexing` (`use_cases/indexing_steps.py`). La Unit of Work (`domain/providers/unit_of_work.py`) expose donc aussi `corpus` et `indexations`.

**Retrait** (`use_cases/withdraw_document/`, `WithdrawDocumentHandler(*, unit_of_work)`, `DELETE /api/v1/knowledge-base/documents/{id}`) : une transaction — `KnowledgeDocument.withdraw()` (les versions gardent leur état), item `extract_version` des versions `RECEIVED`, `EXTRACTED`, `FAILED` annulé, `cancel_indexing` sur toutes les versions, item `purge_document` (dedup `purge_document:<document_id>`, payload `{"document_id"}`). Aucune synchronisation Qdrant : le contrôle SQL final de la recherche écarte le document dès le commit (I12), alors que Qdrant porte encore ses points visibles. Réponses : 202 `{document_id, status: "WITHDRAWN", visibility_revision}` ; 404 `DOCUMENT_NOT_FOUND` ; 409 `DOCUMENT_WITHDRAWN` si le document l'est déjà (sans effet : ni révision, ni seconde purge) ; 409 `CONCURRENT_MODIFICATION` si le verrou optimiste est perdu deux fois (un import de version concurrent est rejoué une fois : la version créée entre-temps n'échappe pas à l'annulation) ; 422 pour un identifiant non UUID. Le document reste comme tombstone.

**Défense en profondeur** (domaine) : `KnowledgeDocument.record_extraction` refuse un document `WITHDRAWN` (`DocumentWithdrawnError`, report de S1) ; `RecordExtraction.apply` l'écartait déjà avant l'agrégat.

**Purge** (`use_cases/purge_document/`, `PurgeDocumentHandler(*, unit_of_work, vector_index, blob_eraser, blob_references, clock)` ; work item `purge_document`, `adapters/controller/tasks/purge_document_handler.py`, file d'ingestion, 5 tentatives, `prepare` sous `keeping_lease`) :

1. `prepare` (hors transaction) : rien si le document est inconnu ou non `WITHDRAWN` ; sinon `delete_points(index_id, PointSelector(document_id))` dans chaque index **vivant** (un index `RETIRED` est supprimé en entier en S9), et rien d'autre ;
2. `apply` (finalisation, sous fencing) : `DocumentPurger.delete_derived_data` — chunks puis unités de toutes les versions du document (`DELETE … USING document_versions`, sans charger de ligne), `purged_at` posée à la première purge (`COALESCE`) ;
3. `erase_unheld_blobs`, appelé par le handler du work item **après** le commit de `ctx.finalize` (jamais par une tentative périmée) : pour l'empreinte de chaque version, `DocumentBlobEraser.delete` sauf si `BlobReferences.holders` n'est pas vide (`purged_at` posée, le document purgé ne se retient plus ; un autre détenteur est inatteignable tant que `UNIQUE(content_hash)` est global, O2). Au mieux : une erreur est journalisée (`warning`), jamais remontée — l'item est déjà `SUCCEEDED` — et le fichier, plus détenu, est repris par `CleanBlobOrphans`.

Ordre de l'ADR 0012 point 6 (points, puis chunks, puis blobs), rétabli par la revue S7 : une panne de Qdrant ne supprime rien d'autre (l'item est rejoué, suppression par filtre). Étape après le commit plutôt qu'un item `delete_blob` : `CleanBlobOrphans` est déjà le filet durable de cette suppression. L'item `FAILED` après 5 tentatives n'a pas de hook métier (réouverture : S8). Replanifier une purge (S8) exige une clé distincte de `purge_document:<document_id>` ou une réouverture explicite : `enqueue` rend l'item existant, même `SUCCEEDED` ([§7.3, précision S7](../../../docs/conception/bounded-contexts/knowledge-base.md#73-conditions-didempotence-des-effets-externes)).

**Références de blob** (`SqlAlchemyBlobReferences`, port `BlobReferences`) : documents dont une version, quel que soit son état, porte l'empreinte **et** dont `purged_at` est nul. Après la purge, le tombstone garde l'empreinte (un réimport reste refusé, 409, O2/O11) mais ne retient plus le fichier.

**Orphelins** (`use_cases/clean_blob_orphans/`, `CleanBlobOrphansHandler(*, inventory, references, eraser, clock)`, composé par `composition.build_clean_blob_orphans_handler(session_factory, settings)`) : un blob est supprimé s'il a plus que `CleanBlobOrphans.grace` (`KB_BLOB_ORPHAN_GRACE_SECONDS`, défaut 86 400 s, `settings.blob_orphan_grace`) et qu'aucun document non purgé ne le porte. Cas typique : un import refusé du contenu d'un document purgé redépose le fichier avant sa transaction. Le déclenchement périodique arrive avec la réconciliation (S8) ; les temporaires `tmp/` sont hors périmètre (S8). Course avec un import du **même** contenu (revue S7) : la suppression passe par `DocumentBlobEraser.delete_if_unchanged(content_hash, blob.revision, still_unreferenced=…)` — fichier renommé vers `quarantine/`, remis à son chemin si sa révision (inode, date en ns) n'est plus celle de l'inventaire ou si les détenteurs, relus fichier retiré, ne sont plus vides ; supprimé sinon. Sans cela, un import commité entre la lecture des références et l'`unlink` perdait son blob (`BLOB_MISSING` terminal, contenu jamais réimportable). Risques restants : [ADR 0012, suivi S7](../../../docs/decisions/0012-original-document-storage-and-purge.md#s7--purge-et-orphelins-2026-11-18).

**Volume** (`FileSystemBlobStore`, seul code autorisé à supprimer un fichier — `architecture/test_withdrawal_purge_structure.py`) : `delete(content_hash) -> bool`, sans condition, pour la purge (les répertoires `<aa>/<bb>` restent) ; `delete_if_unchanged(...)` pour les orphelins (quarantaine sous `KB_BLOB_ROOT/quarantine/<empreinte>-<uuid>`, même volume, pas sous `tmp/` : le renommage garde la date ancienne de l'orphelin et le nettoyage des temporaires de S8 pourrait l'effacer avant sa remise en place) ; `list_blobs()` → `StoredBlobFile(content_hash, modified_at, revision)` (date de modification UTC, `BlobRevision(inode, modified_ns)`) pour chaque fichier de `sha256/<aa>/<bb>/`, jamais `tmp/` ni `quarantine/` ; un fichier au nom inattendu est journalisé et ignoré.

**Qdrant pendant une suppression** (défaut préexistant) : pendant un `delete_points` concurrent, `query_points_groups` peut rendre un hit au payload réduit à `unit_id` ; `QdrantVectorIndex.search` levait `KeyError: 'chunk_id'` (500). Le hit est désormais ignoré (journal `debug`), les autres sont rendus.

**Schéma** : migration `9e4b7d2a6c13` (sur `c43c7e850745`) : `knowledge_base.documents.purged_at timestamptz NULL`, sans index ni réécriture de table.

**Tentative zombie après la purge** : une indexation dont l'item a été annulé par le retrait peut encore écrire ses points après la purge ; ils sont `visible=false` (jamais renvoyés, et écartés par le contrôle SQL). Une nouvelle exécution de `purge_document` les supprime ; depuis S8, la réconciliation la replanifie (`purge_document:<id>:<n>`).

### S9 — benchmark des embeddings et migration blue/green

Changer de profil d'embeddings (ou de politique de découpage) sans interrompre la recherche ([roadmap S9](../../../docs/conception/bounded-contexts/knowledge-base-roadmap.md#s9--benchmark-des-embeddings-et-migration-bluegreen), [knowledge-base.md §13, précisions S9](../../../docs/conception/bounded-contexts/knowledge-base.md#13-changement-de-modèle-dembeddings), [ADR 0011](../../../docs/decisions/0011-embedding-profile-search-index-blue-green.md), profil définitif : [ADR 0013, Proposed](../../../docs/decisions/0013-definitive-embedding-profile.md), mesures : [benchmark](../../../docs/conception/bounded-contexts/knowledge-base-embedding-benchmark.md)).

```text
python -m runtime.index_migration start <cible>       ──► StartIndexMigration (corpus FOR UPDATE)
  collection Qdrant du nouvel index (hors transaction), puis UNE transaction :
  index BUILDING + par version PUBLISHED/EXTRACTED d'un document actif :
  Indexation PENDING (politique de l'index) + index_version (lot présent) | rechunk_version (lot à produire)
worker_ingestion : rechunk_version ─► lot (version, politique) ─► index_version ─► sync_visibility (version publiée)
extraction pendant la construction (FOR SHARE) ─► indexation dans CHAQUE index vivant, rechunking si autre politique
python -m runtime.index_migration activate <index_id>  ──► ActivateSearchIndex (corpus FOR UPDATE)
  couverture I11 calculée ; aucune sync_visibility en attente ni dernière FAILED pour un document publié ;
  bascule (I10, ancien RETIRED) + drop_index_collection disponible après KB_INDEX_DROP_DELAY_SECONDS
python -m runtime.index_migration abandon <index_id>   ──► AbandonIndexMigration (corpus FOR UPDATE, revue S9)
  index BUILDING -> RETIRED ; indexations PENDING/FAILED -> CANCELLED ; index_version et rechunk_version
  de l'index annulés (fencing) ; drop_index_collection disponible après KB_INDEX_DROP_DELAY_SECONDS
worker_ingestion : drop_index_collection ─► collection de l'index RETIRED supprimée
```

**Ports** (`domain/providers/`) : `embedder_provider` (`TokenCountingEmbedder`, `EmbedderProvider.for_profile`, `UnknownEmbeddingProfileError`), `index_migration` (`IndexableVersion`, `IndexableVersionReader.list_indexable_versions`, `documents_awaiting_visibility`, `documents_with_failed_visibility` et `indexation_statuses` (revue S9), `MigrationUnitOfWork` = `IndexingUnitOfWork` + `versions`), `index_collections` (`IndexCollections.delete_collection`, port étroit distinct de `VectorIndex`), `extraction_transaction.RechunkTransaction` (+ `chunks`) ; `WorkQueue.enqueue(..., delay=None)` (item disponible après un délai) ; `WorkQueue.enqueue_again(..., reuse_running=True)` (revue S9 : `False` ne réutilise qu'un item `PENDING`, pour `reschedule_visibility_sync`).

**Cas d'usage** : `start_index_migration` (refus sans effet : `NoActiveIndexToMigrateError`, `MigrationInProgressError` — un seul index `BUILDING` à la fois, invariant de l'agrégat depuis la revue S9 (`IndexAlreadyBuildingError`, traduit) et index unique partiel `ix_search_indexes_one_building` —, `NothingToMigrateError` ; même couple rejoué → même index, rien d'écrit), `activate_search_index` (`IncompleteCoverageError` de l'agrégat, puis `SynchronizationPendingError`, puis `SynchronizationFailedError` (revue S9) ; `UnknownIndexError`, `InvalidIndexTransitionError` ; `drop_delay` obligatoire), `abandon_index_migration` (revue S9 : index `BUILDING` → `RETIRED`, son travail d'indexation annulé, `drop_index_collection` planifié ; `UnknownIndexError`, `InvalidIndexTransitionError` ; `drop_delay` obligatoire), `drop_index_collection` (index `RETIRED` seulement, sinon `IndexStillLiveError`), `rechunk_version` (`prepare` re-parse le blob, O4, et découpe selon la politique de l'index avec le tokenizer de son profil ; `apply` écrit le lot une fois, I7, et planifie `index_version` ; `fail` → indexation `FAILED`). Parsing, segmentation et découpage communs : `use_cases/extraction_steps.VersionExtractor` (extraction et rechunking). `RecordExtraction` crée une indexation par index vivant ; pour un index d'une autre politique, `rechunk_version` au lieu de l'ignorer (S4). `CompleteIndexation` et `SearchKnowledge` prennent `embedders=` (le modèle du profil de l'index visé, ou de l'index actif), seule forme depuis la revue S9 (`embedder=` et `token_counter=` supprimés ; les tests à un seul modèle passent un `EmbedderRegistry` d'un profil, `indexing_fakes.single_profile`). Profil non chargeable : `CollectionMismatchError` (indexation, terminal) ou `SearchProfileMismatchError` (recherche, 503 `SEARCH_PROFILE_MISMATCH`), jamais un autre modèle. Indexation sans lot de sa politique : `ChunkBatchMissingError` (`CHUNK_BATCH_MISSING`), jamais une indexation vide.

**Adapters** : `adapters/gateway/embedding/embedder_registry.EmbedderRegistry` (une fabrique par profil, appelée au premier usage, même instance ensuite ; `lazy_sentence_transformers(profils, cache_dir=)`) ; `KnowledgeBaseResources.embedders` (API) et `build_work_handlers` (worker) le construisent sur les profils du catalogue ; `QdrantVectorIndex.delete_collection` (idempotent) ; `SqlAlchemyIndexableVersionReader` (`SqlAlchemyUnitOfWork.versions`, une requête par méthode quel que soit le nombre de versions ; prédicat « version indexable » partagé avec `SqlAlchemyReconciliationStore`, `adapters/gateway/persistence/indexable_versions.py`, revue S9) ; `SqlAlchemyExtractionTransaction.chunks` ; handlers `rechunk_version_handler` et `drop_index_collection_handler` (file d'ingestion, 5 tentatives) ; `composition.build_start_index_migration_handler`, `build_activate_search_index_handler(…, *, drop_delay)` et `build_abandon_index_migration_handler(…, *, drop_delay)` (délai obligatoire ; seuls les tests passent 0), `compose_knowledge_search(…, embedders=)`.

**Catalogue des cibles** (`frameworks/configuration/embedding_targets.py`) : `CATALOGUE` `nom -> MigrationTarget(profile, chunking_policy)` (profils `MULTILINGUAL_E5_BASE` et `MULTILINGUAL_E5_SMALL` définis dans `domain/value_objects/embedding_profile.py`, revue S9) (politique comptée par le tokenizer du profil, budget sous sa longueur maximale, vérifiés à la construction) ; `migration_target(nom)`, `catalogued_profiles()`. Contenu : voir le [rapport de benchmark](../../../docs/conception/bounded-contexts/knowledge-base-embedding-benchmark.md).

**Réconciliation** : règle distincte de S8, `MISSING_FROM_BUILDING_INDEX` (`building_indexes_missing_version`) : version `PUBLISHED` sans indexation dans un index `BUILDING`, ou `EXTRACTED` sans lot ni indexation de sa politique → indexation `PENDING` + `index_version` ou `rechunk_version` (la règle `EXTRACTED_WITHOUT_INDEXATION` de S8 est inchangée, les deux ne se recouvrent pas). `rechunk_version` `FAILED` sans hook (O14) → indexation `FAILED`. Rejeu (`/failures`) : `rechunk_version` comme `index_version` (indexation `FAILED` → `PENDING`) ; `drop_index_collection` si l'index est `RETIRED` (`OBSOLETE_WORK` sinon).

**Codes** (catalogue S8) : `CHUNK_BATCH_MISSING` (terminal, rejouable après rechunking), `INDEX_STILL_LIVE` (terminal, non rejouable).

**Réglage** : `KB_INDEX_DROP_DELAY_SECONDS` (défaut 300, Proposed) : une recherche lit l'index actif puis interroge Qdrant ; commencée avant la bascule, elle peut encore lire l'ancienne collection.

**Surface publique** : `start_index_migration(session_factory, qdrant_client, target_name) -> UUID`, `activate_search_index(session_factory, index_id, settings=None)`, `abandon_index_migration(session_factory, index_id, settings=None)` (revue S9), `migration_target_names()`, `IndexOperationRefusedError`.

**Refus d'opération** (défaut D2 de la validation globale) : `IndexOperationRefusedError` (`domain/exceptions/corpus_errors.py`, réexportée par la racine) est la base de tous les refus des trois commandes : `KnowledgeCorpusError` (`UnknownIndexError`, `InvalidIndexTransitionError`, `IndexAlreadyBuildingError`, `IncompleteCoverageError`, `DuplicateIndexError`), `StartIndexMigrationError` (`NoActiveIndexToMigrateError`, `MigrationInProgressError`, `NothingToMigrateError`), `SynchronizationPendingError`, `SynchronizationFailedError` et `UnknownMigrationTargetError` (qui reste une `LookupError`). `python -m runtime.index_migration` l'attrape seule : une ligne `refusé : <motif>` sur stderr, code de sortie 1, sans trace ; toute autre erreur garde sa trace.

**Limites connues** (revue S9) : un démarrage refusé par une migration concurrente laisse une collection Qdrant vide et orpheline ([§16, O17](../../../docs/conception/bounded-contexts/knowledge-base.md#16-points-ouverts)).

**Procédure pas à pas** : [guide de l'environnement local](../../../docs/guides/environnement-local.md#migrer-lindex-de-recherche-knowledge-base-s9).

### S8 — réconciliation et gestion des erreurs

Aucun état bloqué ni écart durable entre PostgreSQL, Qdrant et le volume ([roadmap S8](../../../docs/conception/bounded-contexts/knowledge-base-roadmap.md#s8--durcissement-de-la-réconciliation-et-gestion-des-erreurs), [knowledge-base.md, précisions S8 et §16](../../../docs/conception/bounded-contexts/knowledge-base.md#16-points-ouverts)).

```text
beat knowledge_base.work.reconcile (file par défaut) ──► item reconcile_knowledge_base:<créneau> (enqueue)
worker_ingestion, entre deux traitements             ──► ReconcileKnowledgeBase : un cycle, puis SUCCEEDED
  1. points des index vivants (Qdrant, scroll)  PUIS  état SQL de leurs documents
  2. publications perdues ; EXTRACTED sans indexation ; manifests échantillonnés
  3. items FAILED sans état métier (O14) ; purges FAILED (réouverture bornée)
  4. quarantaine ; blobs orphelins ; temporaires tmp/ ; clés d'idempotence > 24 h (O15)
GET  /api/v1/knowledge-base/failures            ──► items FAILED (hors cycles), du plus récent au plus ancien
POST /api/v1/knowledge-base/failures/{id}/retry ──► item rouvert + version / indexation remise en attente (202)
```

**Catalogue des codes** (`domain/value_objects/error_catalog.py`) : `ErrorSpec(code, automatic_retry, replayable)`, `ERROR_CATALOG`, `spec_of`, `is_replayable`, `retries_automatically` (code inconnu → `UnknownErrorCodeError`), `is_known_replayable` (absent ou inconnu → faux, pour ce qui ne doit pas lever : liste des échecs, hooks). Un code n'est défini que là (`architecture/test_reconciliation_structure.py`) ; `work_queue`, `extract_version_handler`, `complete_indexation.errors`, `ParseErrorCode` le réexportent ; `NOT_RETRYABLE_CODES` est une vue dérivée. Les hooks d'échec d'`extract_version` **et** d'`index_version` écrivent `retryable` d'après le catalogue (`INVALID_PAYLOAD` ne sort plus « rejouable »). Revue S8 : `TOO_MANY_PAGES`, `DOCUMENT_TOO_LARGE` (bornes), `UNKNOWN_KIND`, `NOT_FINALIZED` (code déployé) sont rejouables, sans réessai automatique (O2 interdit de réimporter le même contenu) ; `TerminalWorkError(code)` lève `UnknownErrorCodeError` pour un code hors catalogue, et `test_every_terminal_work_error_carries_a_catalog_code` vérifie chaque usage.

**Réconciliation** (`use_cases/reconcile_knowledge_base/`) : `ReconcileKnowledgeBaseHandler` orchestre `PointReconciliation`, `IndexationReconciliation`, `FailedWorkReconciliation` et `StorageHousekeeping` ; chaque jugement est une règle pure de `domain/services/reconciliation.py` (`judge_point`, `plan_point_corrections`, `compare_manifest`, `points_beyond_manifest`, `indexes_missing_indexation`, `is_publishable_completed`, `decide_failed_item`) ; chaque correction a sa transaction et relit l'état avant d'écrire (une transaction par document replanifié ; un document en échec n'empêche pas les autres, les échecs sont relevés ensemble à la fin) ; le travail long (réindexation, synchronisation, purge) est planifié, pas exécuté. `ReconciliationReport(counts)` : décompte par `DiscrepancyKind` de ce qui a été **trouvé** (`clean` au cycle suivant) ; une ligne de journal par cycle. Composition : `composition.build_reconcile_knowledge_base_handler(session_factory, vector_index, settings, *, clock=None, sampler=None)`.

| Écart (`DiscrepancyKind`) | Correction |
|---|---|
| `ORPHAN_POINTS` (points) | document ou version inconnus, version `SUPERSEDED`/`DISCARDED` : `delete_points` par filtre ; hors manifest d'une version échantillonnée : `delete_point_ids` |
| `HIDDEN_PUBLISHED_POINTS` (points) | version `PUBLISHED` de document `ACTIVE`, `visible=false`, aucune synchronisation en attente : `sync_visibility` replanifiée (génération suivante) |
| `WITHDRAWN_DOCUMENT_POINTS` (points) | document retiré **et purgé** : `purge_document:<id>:<n>` ; retiré non purgé = purge en attente, pas un écart |
| `COMPLETED_NOT_PUBLISHED` | `PublishDocumentVersion` (I5, I9 revérifiés) ; compté seulement si la publication aboutit (un refus est journalisé, pas compté) |
| `EXTRACTED_WITHOUT_INDEXATION` | indexation `PENDING` + `index_version` dans chaque index vivant de la politique du lot (corpus `FOR SHARE`) |
| `MANIFEST_DRIFT` | `Indexation.reopen()` (`COMPLETED` → `PENDING`) + `index_version` (génération suivante) ; pour une version déjà publiée, seuls les points absents ou divergents sont réécrits (les autres restent visibles) et la finalisation replanifie `sync_visibility` |
| `FAILED_WORK_WITHOUT_STATE` (O14) | version `FAILED` / indexation `FAILED` avec code, message, `retryable` du catalogue ; item jamais rouvert |
| `FAILED_PURGE` | item rouvert (≤ `KB_RECONCILE_MAX_PURGE_REOPENINGS`, `reopen_count` relu sous le verrou de l'item), sinon laissé au rejeu explicite et compté à chaque cycle |
| `QUARANTINED_BLOB` | détenu et chemin libre : remis en place ; sinon supprimé après le délai de grâce (`ctime`) |
| `ORPHAN_BLOB`, `STALE_UPLOAD` | `CleanBlobOrphans` (S7), `CleanStaleUploads` (`tmp/`), délai `KB_BLOB_ORPHAN_GRACE_SECONDS` |
| `EXPIRED_IDEMPOTENCY_KEY` | clés de plus de `KB_IDEMPOTENCY_KEY_TTL_SECONDS` supprimées |

**Ordre de lecture** : Qdrant d'abord (`PointInventory.scan_points`, `QdrantVectorIndex` : `scroll` paginé de 256, payload réduit, sans vecteur ; points regroupés par document, version, visibilité : la mémoire suit le nombre de versions), puis PostgreSQL. Un point écrit après une lecture SQL antérieure serait pris pour un orphelin ; une version ne disparaît jamais de PostgreSQL. Vérifié par une publication pendant le parcours (`test_a_document_published_while_the_cycle_runs_is_never_taken_for_an_orphan`).

**Work item** (`adapters/controller/tasks/reconcile_handler.py`) : `ReconcileWorkHandler` (`reconcile_knowledge_base`, file d'ingestion, 3 tentatives, cycle sous `keeping_lease`, un `ctx.finalize` qui n'écrit rien, pas de hook) ; `enqueue_reconciliation(session_factory, settings, *, now=None)` : clé `reconcile_knowledge_base:<floor(epoch / intervalle)>`, payload vide ; rend l'item d'un cycle encore `PENDING` ou `RUNNING` sans rien créer (`SqlAlchemyWorkQueue.live_item`) ; `build_reconcile_work_handler(session_factory, settings, *, vector_index)`. La tâche beat `knowledge_base.work.reconcile` (file par défaut) ne fait qu'enregistrer l'item. Le créneau est calculé avec l'horloge du worker : ce n'est qu'une clé de dédoublonnage. Si `worker_ingestion` est occupé plus d'un intervalle (un PDF de 300 pages : ~15 min), les créneaux manqués ne s'accumulent pas (revue S8) : le cycle en attente fait le travail de tous. Un cycle `FAILED` n'est ni listé ni rejouable (409 `NOT_REPLAYABLE`, `SupersededCycleError`) : le créneau suivant en planifie un neuf.

**Générations** (`WorkQueue.enqueue_again`, `SqlAlchemyWorkQueue`) : un travail à refaire après un item terminé prend `<clé>:<n>` (n = plus haute génération + 1), sauf si un membre de la famille est `PENDING`/`RUNNING` (rendu, sans doublon) ; `cancel(clé)` annule la famille (`dedup_key = clé OR dedup_key LIKE 'clé:%'`, motifs échappés). `test_rescheduling_a_completed_purge_under_its_key_creates_no_work` reste vrai.

**Rejeu et consultation** (`use_cases/retry_ingestion/`, `use_cases/list_failures/`, `adapters/controller/rest/failures_router.py`, fabriques `build_retry_ingestion_handler(session_factory)` et `build_list_failures_handler(session_factory)`, port `FailedWorkRepository` — `SqlAlchemyFailedWorkRepository` sur la session de `SqlAlchemyUnitOfWork.failed_work`) :

- `GET /api/v1/knowledge-base/failures?limit=` (1 à 500, défaut 100) → 200 `{"items": [{item_id, kind, code, message, attempts, max_attempts, failed_at, replayable, document_id, version_id, index_id}]}`, hors cycles `reconcile_knowledge_base` (filtrés avant `limit`) ;
- `POST /api/v1/knowledge-base/failures/{item_id}/retry` → 202 `{item_id, kind, status: "PENDING"}` ; 404 `FAILURE_NOT_FOUND` ; 409 `NOT_FAILED` (`status`), `NOT_REPLAYABLE` (`failure_code` ; code non rejouable ou inconnu, ou cycle de réconciliation), `OBSOLETE_WORK` (`reason` : version supplantée, document retiré, index retiré, indexation annulée), `CONCURRENT_MODIFICATION` ; 422 pour un identifiant invalide. Ligne de l'item verrouillée (`FOR UPDATE`) : deux rejeux simultanés → un 202, un 409 `NOT_FAILED` ; un rejeu et une adoption O14 simultanés passent l'un après l'autre (vérifié par barrière, `test_s8_failed_work`). Statut d'un item : `WorkItemStatus` (`domain/providers/failed_work.py`).

**Borne de volume (O16)** : `RecordExtractionHandler(..., max_document_tokens=None)` ; après la segmentation, avant le chunking et toute écriture, somme des `token_count` des unités > borne → `DocumentTooLargeError` (`use_cases/errors.py`, partagé avec le rechunking depuis la revue S9) → `TerminalWorkError("DOCUMENT_TOO_LARGE")`, version `FAILED`, rejouable (revue S8) : relever `KB_MAX_DOCUMENT_TOKENS`, redémarrer `worker_ingestion`, puis `POST /failures/{id}/retry`. `build_extract_version_handler` passe `settings.max_document_tokens` (défaut 1 000 000, origine : [§16, O16](../../../docs/conception/bounded-contexts/knowledge-base.md#16-points-ouverts)).

**Schéma** : migration `5d2f8c1a7b30` (sur `9e4b7d2a6c13`) : `work_items.reopen_count integer NOT NULL DEFAULT 0`, `CHECK >= 0` (défaut constant : pas de réécriture ; le `CHECK` parcourt la table sous ACCESS EXCLUSIVE, quelques milliers de lignes en V1).

**Mémoire** : `worker_ingestion` remplace son enfant toutes les `KB_INGESTION_MAX_TASKS_PER_CHILD` tâches (50, `compose.yaml`) : décision prise sur la mesure de la revue S6 (pic de RSS croissant sur des conversions PDF successives), sans nouvelle mesure. Les cycles de réconciliation comptent (12 par heure à vide : un enfant toutes les ~4 h 10 min) ; les modèles étant paresseux, un enfant recyclé à vide ne recharge rien avant le traitement suivant (≤ ~9 s). Défaut conservé (revue S8, M9).

**Hors V1** : purge des work items terminés (Open en S0) ; parcours complet des points à chaque cycle non mesuré sur un grand corpus (coût linéaire en nombre de points, 256 par requête `scroll`).

## Surface publique

`knowledge_base/__init__.py` réexporte, depuis `adapters/controller/dependencies.py` :

| Nom | Rôle |
|---|---|
| `register(app, resources)` | monte les routes d'import, de retrait (S7), de recherche et de consultation et rejeu des échecs (S8) sur l'application FastAPI, à sa construction ; `resources` est un fournisseur de `KnowledgeBaseResources`, appelé à chaque requête (les ressources sont créées par `runtime` dans le `lifespan`) ; réglages et modèle de la recherche viennent des ressources (une seule source depuis la revue S5) |
| `KnowledgeBaseResources(session_factory, settings, qdrant_client)` | ressources de l'API : fabrique de sessions du runtime, `KnowledgeBaseSettings` relu à chaque construction d'application, client Qdrant du processus (S5) ; portent aussi les modèles paresseux de la recherche (`embedders`, un par profil du catalogue depuis S9, hors constructeur, interne au contexte) |
| `build_knowledge_search(resources)` | implémentation du contrat public `knowledge_base.contracts.providers.KnowledgeSearch` (S5) sur les modèles des ressources (celui du profil de l'index actif), partagés avec la route ; ne se connecte à rien, ne charge pas de modèle. Variante interne pour les tests : `adapters/controller/composition.compose_knowledge_search(session_factory, qdrant_client, settings, *, embedders | embedder et token_counter)` |
| `register_worker(celery_app, resources, settings=None)` | déclare les tâches `knowledge_base.work.{execute,dispatch,recover,reconcile}`, leurs routes et le `beat_schedule`, dans le processus maître ; `resources` est un fournisseur appelé seulement à l'exécution d'une tâche |
| `KnowledgeBaseWorkerResources(session_factory, loop, handlers=())` | ressources d'un processus enfant : fabrique de sessions, boucle asyncio persistante, handlers (registre construit à la création) |
| `KnowledgeBaseSettings` | réglages `KB_*` (pydantic-settings) |
| `WorkHandler` | Protocol d'un handler : `kind`, `queue`, `max_attempts` (appliqué au claim), `execute(item, ctx)` ; hook facultatif `on_terminal_failure(session, item, error)` |
| `build_work_handlers(session_factory, qdrant_client, settings=None)` | handlers de production des work items (`extract_version`, `index_version`, `sync_visibility`, `purge_document`, `reconcile_knowledge_base`, `rechunk_version`, `drop_index_collection`), construits par `runtime` dans chaque processus enfant sur sa fabrique de sessions et son client Qdrant ; ne se connecte à rien et ne charge aucun modèle (chacun à la première tâche qui en a besoin) |
| `provision_search_index(session_factory, qdrant_client)` | crée et active l'index initial s'il n'existe pas (idempotent) ; rend l'index actif ; appelé par `python -m runtime.provision` au démarrage de la stack ; ne démarre jamais de migration |
| `start_index_migration(session_factory, qdrant_client, target_name)` | démarre une migration blue/green vers une cible du catalogue (S9) ; rend l'index `BUILDING` ; rejouable |
| `activate_search_index(session_factory, index_id, settings=None)` | bascule la recherche sur l'index `BUILDING` (couverture et synchronisations vérifiées), collection de l'ancien supprimée après `KB_INDEX_DROP_DELAY_SECONDS` (S9) |
| `migration_target_names()` | noms des cibles du catalogue (aide de `python -m runtime.index_migration`) |
| `IndexOperationRefusedError` | base des refus d'opération sur les index (provisionnement, démarrage, activation, abandon d'une migration) : sans effet, message destiné à l'opérateur |

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
  use_cases/indexing_steps.py             # kinds index_version / sync_visibility (S4) / purge_document (S7), clés, enqueue ; cancel_indexing (S7)
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
  adapters/controller/composition.py      # build_provision_search_index_handler (S4), compose_knowledge_search (S5), build_clean_blob_orphans_handler (S7)
  adapters/controller/rest/               # routes d'import (S2) et de retrait (S7), schémas HTTP, traduction des refus, KnowledgeBaseResources
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
  adapters/gateway/blob/filesystem_blob_store.py  # FileSystemBlobStore : put (S2), read (S3), delete, list_blobs (S7)
  adapters/gateway/parsing/               # RoutingDocumentParser, DoclingMarkdownParser, PlainTextParser (S3) ; docling_mapping, docling_conversion, DoclingPdfParser, DoclingDocxParser (S6)
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
  domain/providers/                       # S7 : document_blob_eraser, document_blob_inventory, blob_references, document_purger (DocumentPurger, PurgeTransaction)
  use_cases/withdraw_document/            # WithdrawDocument, WithdrawDocumentHandler, WithdrawDocumentResult (S7)
  use_cases/purge_document/               # PurgeDocument, PurgeDocumentHandler (prepare, apply), PreparedPurge (S7)
  use_cases/clean_blob_orphans/           # CleanBlobOrphans, CleanBlobOrphansHandler (S7)
  adapters/controller/tasks/purge_document_handler.py  # PurgeDocumentWorkHandler, build_purge_document_handler (S7)
  adapters/gateway/persistence/sqlalchemy_document_purger.py, sqlalchemy_blob_references.py  # S7
  domain/value_objects/error_catalog.py   # catalogue unique des codes d'erreur (S8)
  domain/value_objects/point_record.py    # PointRecord, DTO de PointInventory (S8)
  domain/services/reconciliation.py       # règles pures de la réconciliation, DiscrepancyKind (S8)
  domain/providers/                       # S8 : point_inventory, staged_upload_store, blob_quarantine, failed_work, reconciliation_store
  use_cases/reconcile_knowledge_base/     # ReconcileKnowledgeBase, handler, étapes, ReconciliationReport (S8)
  use_cases/retry_ingestion/              # RetryIngestion, RetryIngestionHandler, RetryIngestionResult, refus (S8)
  use_cases/list_failures/                # ListFailures, ListFailuresHandler, FailureView (S8)
  use_cases/clean_stale_uploads/          # CleanStaleUploads, CleanStaleUploadsHandler (S8)
  use_cases/errors.py                     # DocumentTooLargeError (S8, O16 ; déplacé en revue S9)
  adapters/controller/tasks/reconcile_handler.py  # ReconcileWorkHandler, enqueue_reconciliation, build_reconcile_work_handler (S8)
  adapters/controller/rest/failures_router.py, failures_schemas.py  # GET /failures, POST /failures/{id}/retry (S8)
  adapters/gateway/persistence/sqlalchemy_failed_work_repository.py, sqlalchemy_reconciliation_store.py  # S8
  domain/providers/                       # S9 : embedder_provider, index_migration, index_collections ; RechunkTransaction
  use_cases/extraction_steps.py           # VersionExtractor : parsing, segmentation, découpage (extraction et rechunking, S9)
  use_cases/start_index_migration/, activate_search_index/, drop_index_collection/, rechunk_version/  # S9
  use_cases/abandon_index_migration/      # AbandonIndexMigration, AbandonIndexMigrationHandler (revue S9)
  adapters/controller/tasks/rechunk_version_handler.py, drop_index_collection_handler.py  # S9
  adapters/gateway/embedding/embedder_registry.py  # EmbedderRegistry : un modèle paresseux par profil (S9)
  adapters/gateway/persistence/sqlalchemy_indexable_version_reader.py  # S9
  adapters/gateway/persistence/indexable_versions.py  # prédicat « version indexable » partagé (revue S9)
  frameworks/configuration/embedding_targets.py  # CATALOGUE des cibles de migration (S9)
  frameworks/sqlalchemy/tables.py         # MetaData(schema="knowledge_base"), work_items, documents, document_versions, idempotency_keys, knowledge_units, document_chunks, knowledge_corpus, search_indexes, indexations
  frameworks/configuration/chunking.py    # DEFAULT_CHUNKING_POLICY (tokenizer du profil depuis S4)
  frameworks/configuration/settings.py    # KnowledgeBaseSettings
```

## Écrire un handler (slices S2 et suivantes)

1. Effets externes **idempotents**, hors transaction ; `await ctx.heartbeat()` au moins toutes les `ctx.heartbeat_interval` pour un traitement long, de préférence par `keeping_lease(ctx, travail(), intervalle)` qui annule le travail dès qu'un heartbeat est refusé (aucun effet externe d'une tentative périmée après son heartbeat suivant ; l'appel en cours, lui, peut aboutir) ; travail CPU dans `asyncio.to_thread`.
2. `await ctx.finalize(callback)` : `callback(session)` écrit l'état métier ; le passage `SUCCEEDED` est ajouté dans la même transaction, conditionné au fencing token. Un token périmé annule toute la transaction (`StaleAttempt`).
3. Erreurs : `TerminalWorkError(code, message)` → `FAILED` ; `TransientWorkError` et toute autre exception → nouvelle tentative avec backoff tant que `attempts < max_attempts`. Un handler qui se termine sans `ctx.finalize` est un échec terminal `NOT_FINALIZED` (le hook est appelé).
4. Aucune écriture PostgreSQL métier hors de `ctx.finalize` (ou du hook `on_terminal_failure`, exécuté dans la transaction de `fail`).
5. Journal d'une tentative en échec (`execution.log_failed_attempt`, défaut D5) : une `TerminalWorkError` (échec métier attendu, code du catalogue : `OCR_REQUIRED`, `EMPTY_DOCUMENT`…) donne **une** ligne WARNING avec son code et son message (`%r`), sans trace, même chaînée à l'erreur du parseur ; une `TransientWorkError` reste un WARNING avec trace ; toute autre exception (`UNEXPECTED_ERROR`) et l'erreur d'un hook `on_terminal_failure` sont des ERROR avec trace.
6. Si `on_terminal_failure` lève : la transaction de `fail` est annulée (aucune écriture du hook), puis nouvelle tentative avec backoff et le code `TERMINAL_HOOK_FAILED` ; à la dernière tentative, `FAILED` (`TERMINAL_HOOK_FAILED`) **sans** hook. L'état métier de ce cas, comme celui d'un `FAILED` par `recover`, est le point ouvert O14 ([knowledge-base.md §16](../../../docs/conception/bounded-contexts/knowledge-base.md#16-points-ouverts)).

`max_attempts` : `enqueue(max_attempts=None)` applique le défaut de la colonne (5) ; le **claim** remplace cette valeur par celle du handler du `kind`, dans le même `UPDATE`. `retry_later` et `recover` lisent la colonne.

En S0, `finalize` et `on_terminal_failure` reçoivent une `AsyncSession` ; l'Unit of Work du contexte (`SqlAlchemyUnitOfWork`) existe depuis S2, côté API.

`FailureInfo` et `FailureReason` restent distincts (pas la même forme) :

- `FailureInfo` (`domain/providers/work_queue.py`, code stable + message tronqué à 2 000 caractères) décrit l'échec d'une **tentative** de work item : colonnes `last_error_*` et argument du hook `on_terminal_failure` (S0 §5) ;
- `FailureReason` (`domain/value_objects/failure_reason.py`, `code`, `message`, `retryable`) est l'état **durable** d'une version `FAILED` ; `retryable` dit si `RetryIngestion` a un sens.

Les handlers `extract_version` et `index_version` traduisent l'un en l'autre dans leur hook, `retryable` valant « rejouable » du catalogue des erreurs (S8 ; faux pour les codes de contenu de `ParseErrorCode`, `CHUNKING_FAILED`, `INVALID_PAYLOAD` et tout code inconnu ; vrai, depuis la revue S8, pour `TOO_MANY_PAGES`, `DOCUMENT_TOO_LARGE`, `NOT_FINALIZED`, `UNKNOWN_KIND`) ; la réconciliation fait la même traduction quand le hook n'a pas abouti (O14). Une fusion ne sera envisagée que si les deux formes convergent.

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
- Unitaires S6 : `adapters/parsing/test_docling_office_mapping.py` (traduction sur des sorties Docling **enregistrées** des fixtures PDF/DOCX, `fixtures/knowledge_base/office/recorded/`, et sur des documents construits à la main), `test_office_parser_failures.py` et `test_pdf_checks_before_layout.py` (chiffré, corrompu, limite de pages, `OCR_REQUIRED`, PDF partiellement scanné, sans modèle), `test_office_routing.py`, `test_docling_pdf_options.py` (OCR désactivé, CPU, révision épinglée, lots d'une page), `test_office_error_codes.py`, `test_office_import_http.py`. Fixtures : `generate_office_fixtures.py` (reportlab, python-docx, environnement éphémère `uv run --no-project`, sorties identiques octet pour octet) et `record_docling_documents.py`.
- Intégration S6 : `test_office_extraction` (PDF et DOCX équivalents aux Markdown → mêmes unités, pages, tableaux, `OCR_REQUIRED`, chiffré/corrompu, plus de 300 pages, heartbeats pendant l'extraction), `test_office_parsers_live` (parseur vivant = sorties enregistrées, limite incluse, mesures d'extraction), `test_retrieval_evaluation_office`, `test_retrieval_long_units`.
- Unitaires S7 : `domain/test_knowledge_document_s7_states.py`, `use_cases/test_{withdraw_document,import_version_cancels_indexing,purge_document,clean_blob_orphans}.py` (doubles `use_cases/s7_support.py`), `test_purge_document_work_handler.py`, `test_withdraw_document_routes_http.py`, `test_blob_orphan_settings.py`, `adapters/test_filesystem_blob_store_maintenance.py`, `adapters/vector_store/test_search_during_deletion.py` ; `architecture/test_withdrawal_purge_structure.py`.
- Intégration S7 (PostgreSQL, Qdrant et volume réels, `HashEmbedder`, barrières explicites sans délai, `s7_support.py`) : `test_s7_concurrent_versions` (N finit après l'import de N+1 : jamais publiée, travail annulé, points invisibles puis supprimés ; recherches pendant une republication, fenêtre « deux versions visibles dans Qdrant » tenue ouverte), `test_s7_withdrawal_purge` (retrait immédiat, aucune recherche commencée après le commit ne rend le document, purge des points / chunks / blobs, tombstones, rejeu, Qdrant injoignable, zombie après purge, extraction finissant après le retrait, import de version et retrait concurrents dans les deux ordres), `test_s7_blob_orphans`, `test_document_purger` (date de la première purge conservée), `test_purge_migration` (aller-retour ancré sur `9e4b7d2a6c13`).
- Unitaires S8 : `test_error_catalog.py`, `test_error_catalog_extraction_hook.py` (catalogue, deux axes, réexports, hook), `domain/test_reconciliation_rules.py` et `domain/test_reconciliation_bounds.py` (règles pures, réouverture bornée, points hors manifest), `domain/test_indexation_reopen.py`, `test_reconciliation_report.py`, `test_reconcile_work_handler.py`, `test_register_worker_reconcile.py`, `test_reconcile_settings.py`, `test_failure_routes_http.py`, `test_document_volume_limit.py`, `use_cases/test_clean_stale_uploads.py`, `use_cases/test_reindex_published_version.py`, `adapters/test_filesystem_staged_uploads.py` ; architecture : `test_reconciliation_structure.py` (forme des use cases, règles pures, aucun accès au système de fichiers, code d'erreur défini une seule fois).
- Intégration S8 (`s8_support.py`, harnais de S7 : chaque écart est injecté, détecté, corrigé, puis un cycle propre) : `test_s8_reconciliation`, `test_s8_reconciliation_decisions` (réouverture bornée, générations de purge et de réindexation, quarantaine, points hors manifest), `test_s8_failed_work` (O14, aucune boucle), `test_s8_retry_ingestion`, `test_s8_failures_endpoint`, `test_s8_housekeeping`, `test_s8_reconcile_work_item`, `test_s8_point_scan`, `test_s8_document_volume`, `test_work_queue_generations`, `test_reopen_count_migration`.
- Unitaires S9 : `use_cases/test_{start_index_migration,activate_search_index,abandon_index_migration,drop_index_collection,rechunk_version,record_extraction_rechunk,complete_indexation_per_profile,search_per_profile,s9_activation_guards}.py` (doubles `use_cases/s9_support.py`), `adapters/embedding/test_embedder_registry.py`, `domain/test_reconciliation_rules_s9.py`, `test_s9_public_surface.py`, `tests/unit/test_runtime_index_migration.py`.
- Intégration S9 (`s9_support.py`, PostgreSQL, Qdrant et work queue réels, deux profils `HashEmbedder` de 16 et 24 dimensions) : `test_s9_migration` (backfill, rechunking, import pendant la construction, activation, retrait, suppression), `test_s9_concurrency` (verrous `FOR UPDATE` dès la première lecture, extraction et publication en vol, sans `sleep`), `test_s9_search_continuity` (recherches en boucle pendant la bascule et la suppression), `test_s9_reconciliation_and_workers` (règle `BUILDING`, publication perdue après la bascule, profil non chargeable), `test_s9_guards_and_failures` (synchronisation en attente ou en échec, délai de suppression, échecs et rejeux de `rechunk_version` et `drop_index_collection`, `CHUNK_BATCH_MISSING`), revue S9 : `test_s9_abandon` (abandon, annulation sous fencing, nouvelle migration ensuite), `test_s9_indexable_versions` (prédicat partagé, dernière synchronisation en échec, statuts d'indexation), `test_one_building_index_migration` (index unique partiel), deux démarrages concurrents dans `test_s9_concurrency`, `reuse_running` dans `test_work_queue_generations`. Benchmark : `backend/scripts/benchmark_embeddings.py` (`make benchmark-embeddings`).
- Tests `model` : cache des poids `SENTENCE_TRANSFORMERS_HOME` (`make test-integration` le pointe vers `backend/.cache/models`, ignoré par git) ; premier lancement : téléchargement d'environ 1,1 Go. Depuis S6, poids de Docling PDF dans `HF_HOME` (`backend/.cache/huggingface`, ~506 Mio au premier lancement).
