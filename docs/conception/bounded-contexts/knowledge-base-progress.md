# Knowledge Base — Suivi de l'objectif global

> **Objectif :** *Implement Knowledge Base — Document Ingestion & Semantic Retrieval*
> (S0 à S9, une seule branche `feature/knowledge-base-v1`, une seule PR finale).
>
> Ce fichier remplace la commande `/goal`, absente de l'environnement Claude
> Code utilisé (constat du 2026-11-17). Il est mis à jour à la fin de chaque
> slice. Référence : [feuille de route](knowledge-base-roadmap.md).

## Statut global

**En cours** — clôture conditionnée à : S0–S9 implémentées, tests requis
passés, documentation synchronisée, review indépendante réalisée, anomalies
bloquantes corrigées, PR globale prête (sans merge automatique).

Review indépendante : Codex **non disponible** dans l'environnement (ni CLI
`codex`, ni application, constat du 2026-11-17). Si cela ne change pas,
l'objectif terminera en **Pending Independent Review** avec un dossier de
review prêt.

## Jalons

| Slice | Objectif | Dépend de | Statut | Commit(s) | Tests exécutés |
|---|---|---|---|---|---|
| S0 | Infrastructure de traitement durable | — | **Terminée** | voir journal | Ruff, mypy (68 fichiers), 130 unit./archi., 109 intégration, Compose (9 services) |
| S1 | `KnowledgeDocument` et `DocumentVersion` | S0 | **Terminée** | voir journal | Ruff, mypy (86 fichiers), 233 unit./archi., 8/8 mutations détectées |
| S2 | Import Markdown/TXT, stockage, dédoublonnage | S0, S1 | **Terminée** | voir journal | Ruff, mypy (125 fichiers), 309 unit./archi., 162 intégration, import réel sur la stack de dev (202 puis 200) |
| S3 | Docling Markdown, `KnowledgeUnit`, chunking | S2 | **Terminée** | voir journal | Ruff, mypy (180 fichiers), 529 unit./archi., 192 intégration, extraction réelle sur la stack de dev (3 unités QA, 3 chunks) |
| S4 | Embeddings, Qdrant, index initial, publication | S3 | **Terminée** | voir journal | Ruff, mypy (252 fichiers), 759 unit./archi., 273 intégration (18 `model`), 3/3 mutations, import réel jusqu'à PUBLISHED (3 points visibles) |
| S5 | Recherche sémantique, provenance, évaluation | S4 | **Terminée** | voir journal | Ruff, mypy (299 fichiers), 1 115 unit./archi., 321 intégration (dont `retrieval`), 4 mutations tuées, recherche réelle sur la stack de dev (citation complète) |
| S6 | PDF et DOCX avec Docling | S3, S5 | À faire | | |
| S7 | Versions concurrentes, retrait, purge | S4, S5 | À faire | | |
| S8 | Réconciliation et gestion des erreurs | S7 | À faire | | |
| S9 | Benchmark embeddings, blue/green | S5, S8 | À faire | | |

Ordre d'exécution retenu (compatible avec le graphe de dépendances) :
S0 → S1 → S2 → S3 → S4 → S5 → S6 → S7 → S8 → S9.

## Méthode par slice

Relecture des documents et ADR concernés → tests rouges (`expert-tdd`) →
implémentation (`expert-python`) → revue (`tech-lead-reviewer`) → corrections
→ Ruff, mypy strict, unitaires, architecture, intégration → documentation →
commit → mise à jour de ce fichier. Un seul agent à la fois sur un même
périmètre.

## Journal

- 2026-11-17 — Audit initial : `main` à `447a234` (PR #1 et #2 mergées) ;
  socle Phase 1 vert (Ruff, mypy strict sur 28 fichiers, 35 tests unitaires
  et d'architecture) ; aucun code `knowledge_base`. Branche créée.
- 2026-11-18 — S0 terminée : tests rouges (expert-tdd) → implémentation
  (expert-python) → revue (1 bloquant, 4 importants, 6 mineurs, tous corrigés)
  → revalidation : 130 unitaires/architecture, 109 intégration, Compose 9 services.
- 2026-11-18 — S1 terminée : domaine pur (I1–I6), 96 tests, 8 mutations
  détectées ; relecture directe (lot court) au lieu d'un agent de revue.
- 2026-11-18 — S2 et S3 terminées (voir « Corrections en attente », revues faites
  et corrigées dans `1e14342`).
- 2026-11-18 — S4 terminée : tests rouges → implémentation (expert-python) →
  revalidation par l'orchestrateur : Ruff, mypy 252 fichiers, 759 unitaires,
  273 intégration. Mesures : image `production` 152 → 479 Mo (contenu),
  RSS worker ~1 Gio, 91 ms/chunk en conteneur. Revue S4 faite et corrigée (`de9c5b4`).
- 2026-11-18 — S5 terminée : tests rouges (prouvés par une implémentation
  jetable hors dépôt, 18 mutations) → implémentation → revalidation :
  1 115 unitaires, 321 intégration. Recall@5 0,881, MRR 0,853 (42 requêtes) ;
  recherche HTTP médiane 69 ms ; RSS de l'API 146 → 1 267 Mio après le
  premier appel (modèle chargé paresseusement).

## Écarts et décisions en cours de route

- S0 — `create_celery_app` dans `runtime/worker_factory.py` (précision d'implémentation balisée en S0 §10).
- S0 — plafond `max_in_flight` par **file** (conforme §8.2), approximatif sous dispatch concurrent.
- S0 — `max_attempts` du handler appliqué au claim (précision balisée S0 §2/§7).
- S0 — hook d'échec qui lève → `retry_later` `TERMINAL_HOOK_FAILED` ; point **O14** ouvert (S8).
- S0 — scénario 14 : redélivrance par `consumer_timeout` de RabbitMQ 4.4.0 non observée ; seul l'invariant « un seul effet » est prouvé.
- S1 — `record_extraction` n'interdit pas un document `WITHDRAWN` : la protection repose sur l'annulation des work items au retrait (fencing) ; à vérifier en S3/S7.
- S1 — `failure` effacé lors de `FAILED → DISCARDED` (motif perdu) ; acceptable, l'historique d'erreur reste dans `work_items.last_error_*`.
- S2 — O2 tranché : 409 `DUPLICATE_CONTENT` pour un import ciblé ; import sans cible d'un contenu connu → 200 ; contenu d'un document retiré → 409 `DOCUMENT_WITHDRAWN` (`UNIQUE(content_hash)` global, cohérent avec O11).
- S2 — le corps multipart est mis en tampon par Starlette (disque au-delà de 1 Mio) avant la limite de 20 Mo : borner la taille du corps relève du serveur/proxy.
- S2 — dépendance ajoutée : `python-multipart`.
- S3 — compteur de tokens approximatif (majorant, chunks ~30 % plus courts que nécessaire) jusqu'au tokenizer réel de S4.
- S3 — `RecordExtraction` définit ses propres ports (`DocumentReader`, `ExtractionTransaction`, désormais dans `domain/providers/`) au lieu d'étendre `UnitOfWork`.
- S3 — en-tête contextuel redondant (titre du document répété dans le chemin) : à mesurer en S5.
- S3 — blob manquant = `BLOB_MISSING` terminal.
- S4 — provisionnement initial par une commande dédiée (`python -m runtime.provision`, service Compose `kb_provision`) plutôt qu'au démarrage du worker.
- S4 — `sync_visibility` ne supprime que les points des versions SUPERSEDED/DISCARDED (précision S4 au §11).
- S4 — `HF_HUB_DISABLE_XET=1` : le téléchargement xet du Hub est resté bloqué à 0 octet (hôte et conteneur).
- S4 — la FK simple `unit_id` de S3 est conservée à côté de la FK composite.
- S4 — import de N+1 n'annule pas encore les indexations de N (S7) ; pas de backfill de l'identité `approx-xlmr-v1` (S9).

- S5 — `complete=false` seulement si des rejets SQL ont eu lieu et qu'il manque des passages après complément ; complément sur les manquantes × f, rejetés et retenus exclus.
- S5 — index actif absent ou de profil différent → 503 (`NO_ACTIVE_SEARCH_INDEX`, `SEARCH_PROFILE_MISMATCH`) ; erreurs publiées dans `contracts/`.
- S5 — question comptée par le compteur approximatif (majorant) dans le budget de passage.
- S5 — le jeu de référence n'exerce pas la troncature (toutes les unités tiennent en un chunk) : `max_passage_tokens` reste Proposed, à remesurer en S6.
- S5 — l'API charge E5 (≈ 1,1 Gio de RSS supplémentaire) : risque mémoire à suivre avec O5.

## Corrections en attente

Revue tech lead du commit S2 (`2fc83da`) : aucun bloquant. Corrections
appliquées après S3 (2026-11-18), non commitées à ce stade :

- I-1 — verrou optimiste : la première lecture d'un document dans une UoW fait foi (`get` ne rafraîchit plus `lock_version`) ; `find_by_content_hash` rend un modèle de lecture `ContentOwner(document_id, document_status, version_id, version_number)` ; tests d'intégration avec commit concurrent entre les deux lectures (`test_import_persistence`). **Fait.**
- I-2 — même `Idempotency-Key` concurrente sur `/versions` : `ConcurrentModificationError` inclus dans le rejeu unique (`retry_once_after_race`) ; test d'intégration (deux 202 identiques) et tests unitaires (rejeu de la clé, `DUPLICATE_CONTENT`, import sur l'état à jour). **Fait.**
- I-3 — `knowledge-base.md` §4.1 I3 aligné sur `UNIQUE(content_hash)` global (renvoi O2, précision S2). **Fait.**
- M-1 — libellé « streaming » de la feuille de route corrigé (fait) ; refus précoce sur `Content-Length` à évaluer (non traité).
- M-2 — fabriques des handlers injectables : assemblage dans `adapters/controller/dependencies.py`, `add_document_routes` reçoit les fabriques ; test HTTP sans base (`tests/unit/knowledge_base/test_document_routes_http.py`). **Fait.**
- M-3 — nom de fichier réduit à son nom final (`/` et `\`), refusé s'il est vide, > 255 caractères ou avec caractère de contrôle : 422 `INVALID_FILENAME`. **Fait.**
- M-4 — I1 en base : trigger `BEFORE UPDATE` sur `document_versions` (migration `756db53032ab`, au-dessus de `22df253e1e05`) ; `alembic check` propre. **Fait.** La migration de S4 devra avoir `down_revision = "756db53032ab"`.
- M-5 — `ImportDocumentResult` déplacé dans `use_cases/import_result.py`. **Fait.**
- M-6 — expiration des clés d'idempotence : point O15 [Open] dans `knowledge-base.md` §16. **Fait.**
- M-7 — test HTTP `POST /documents` d'un contenu de document retiré → 409 `DOCUMENT_WITHDRAWN`. **Fait.**
- Tests de migration S2 et S3 ancrés sur leurs révisions (redescente jusqu'à la révision précédente, vérification, `upgrade` de la révision, `upgrade head`). **Fait.**

Revue tech lead du commit S3 (`3cb1ecf`) : aucun bloquant ; corrections
appliquées le 2026-11-18 :

- I-1 découpe linéaire (2 Mo sans blanc : 238,3 s → 2,48 s ; lettres 17,9 s → 0,20 s ; test < 5 s). Fait.
- I-2 identité de politique `version+tokenizer` (précision ADR 0009, section Suivi). Fait.
- Front-matter strict ; `DOCUMENT_TOO_COMPLEX` et `CHUNKING_FAILED` terminaux non rejouables ; goldens enrichis (empreinte des blocs), Docling importé directement ; UUID épinglés ; séparateur `\x00` dans `chunk_hash` ; ports dans `domain/providers/`, politique dans `frameworks/configuration/chunking.py`. Fait.
- Reportés : FK composite chunks → units (migration S4) ; `BLOB_MISSING` terminal mais `retryable=True` alors que `RetryIngestion` n'arrive qu'en S8 (à traiter avec O14).

Revue tech lead du commit S4 (`30f3f60`) : aucun bloquant, 2 importants, 8 mineurs.
Corrections appliquées (2026-11-18) — 770 unitaires, 285 intégration, mutation du heartbeat tuée :

- I1 — tentative zombie : `prepare` périmé poursuivait ses upserts `visible=false` après un heartbeat refusé → annulation du travail + réparation par la réconciliation S8 (périmètre S8 étendu). **Fait.**
- I2 — heartbeat de `index_version` non testé. **Fait.**
- M1 — barrière FOR SHARE / FOR UPDATE et provisionnement concurrent contre PostgreSQL réel. **Fait.**
- M2 — version `EXTRACTED` sans indexation : code de log identifiable pour S8. **Fait.**
- M3 — un seul `Embedder` (E5) par worker : un index d'un autre profil échouerait de façon terminale → fournisseur d'`Embedder` par profil à prévoir en **S9**. Reporté.
- M4 — I11 par booléen `coverage_complete=True` au provisionnement initial : preuve de couverture réelle en **S9** ; Suivi ADR 0011 corrigé. **Fait.**
- M5 — FK différée `active_index_id` → `search_indexes`. **Fait.**
- M6 — surface publique dans 005. **Fait.**
- M7 — composition unique des dépôts UoW / transaction d'indexation. **Fait.**
- M8 — surface sans usage (`PublishDocumentVersionHandler.handle`, champ `visible`) : `handle` retiré, précision §3.6 ; champ `visible` d'`IndexedPoint` conservé jusqu'après S5 (utilisé par les doubles de test). **Partiel.**
