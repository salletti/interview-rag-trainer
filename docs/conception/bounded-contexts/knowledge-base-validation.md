# Knowledge Base — Validation globale finale (S0 à S9)

> Rapport de preuves. Chaque verdict repose sur une commande exécutée pendant
> cette validation ; les sorties citées sont recopiées (résumées, jamais
> réécrites). Ce qui n'a pas pu être exécuté figure dans « Non exécuté ».
> Feuille de route : [knowledge-base-roadmap.md](knowledge-base-roadmap.md) ;
> commandes : [guide environnement local](../../guides/environnement-local.md).

## Contexte

| | |
|---|---|
| Date | 2026-11-19, de 12:10 à 12:46 heure locale (CET) ; les horodatages des journaux des conteneurs sont en UTC (CET − 1 h) |
| Branche, commit | `feature/knowledge-base-v1`, HEAD `91c5b8c` (« fix(knowledge-base): corrections de la revue S9 »), sans modification du code suivi |
| Machine | Apple M5 Pro, 48 Gio, macOS 26.5.2 ; Docker Engine 29.4.0 (linux/arm64, 18 CPU et 7,7 Gio alloués à la VM), Compose 5.1.1 ; uv 0.12.1, Python 3.13.14 |
| Stack de dev | projet Compose `interview-rag-trainer` (API 8010, PostgreSQL 5440) ; volumes conservés, aucun `down -v` (`interview-rag-trainer_models_cache` intact) |
| Stack de test | `compose.test.yaml` (PostgreSQL 5441), détruite après chaque usage |

## Synthèse

| # | Vérification | Verdict |
|---|---|---|
| 1 | Qualité statique (Ruff, format, mypy, pytest unitaires et architecture) | **OK** |
| 2 | `make test-integration` | **OK** |
| 3 | Alembic head → base → head → check sur une base jetable | **OK** |
| 4 | Stack de dev : migrations, santé, scheduler et workers | **OK** (scheduler sans healthcheck, voir D3) |
| 5 | Imports MD, TXT, PDF, DOCX, refus du PDF scanné, recherche, retrait, `/failures` | **OK** |
| 6 | Fiabilité : tests nommés, plus un redémarrage de `worker_ingestion` en cours d'ingestion | **OK** |
| 7 | Qualité de recherche (valeurs mesurées et seuils) | **OK** |
| 8 | Blue/green aller-retour e5-base → e5-small → e5-base, plus abandon | **OK** |
| 9 | Frontières entre BC | **OK** |

---

## 1. Qualité statique — OK

Dans `backend/` :

| Commande | Sortie |
|---|---|
| `uv run ruff check .` | `All checks passed!` |
| `uv run ruff format --check .` | `478 files already formatted` |
| `uv run mypy` | `Success: no issues found in 474 source files` |
| `uv run pytest -q` | `1735 passed, 546 deselected in 8.41s` (les 546 désélectionnés sont les tests `integration`, voir point 2) |
| `uv run pytest -q tests/architecture` | `80 passed in 0.66s` |

**Les 80 tests d'architecture** (`backend/tests/architecture/`) :

| Fichier | Tests | Ce qu'il couvre |
|---|---|---|
| `test_dependency_rules.py` | 35 | Règles par analyse des imports de `src/` : domaine et use cases sans framework ; domaine indépendant des couches externes ; domaine pur de KB (`entities`, `value_objects`, `events`, `exceptions`) limité à la bibliothèque standard ; use cases sans adapters ni frameworks ; **`contexts_never_import_each_other_internals`** (un contexte n'importe d'un autre que `<contexte>.contracts`) ; **`no_context_depends_on_runtime`** ; **`runtime_only_sees_public_surface`** (racine ou `contracts` d'un contexte, jamais ses couches internes) ; `shared` est une feuille. Contexte attendu : `assessment`, `interview_training`, `knowledge_base`. Chaque règle est aussi testée sur un faux arbre de sources, pour vérifier qu'elle détecte bien une violation. |
| `test_knowledge_search_contract.py` | 17 | Façade `knowledge_base.contracts` : protocole `KnowledgeSearch`, types résultats figés ; **`contracts` n'importe que la stdlib et lui-même** ; un autre contexte peut consommer le contrat sans toucher à l'intérieur ; domaine et use cases n'importent pas la façade ; `runtime` ne câble la recherche que par la surface publique. |
| `test_reconciliation_structure.py` | 13 | Structure de la réconciliation (S8). |
| `test_withdrawal_purge_structure.py` | 10 | Structure du retrait et de la purge (S7). |
| `test_embedding_confinement.py` | 3 | Pile d'embeddings confinée à ses adapters. |
| `test_docling_confinement.py` | 2 | `docling`, `docling_core` et `pypdfium2` importés seulement par `adapters/gateway/parsing/docling_*.py`. |

## 2. Intégration — OK

Commande : `make test-integration`, lancée **une seule fois** (11:18:35 → 11:24:18 UTC). Elle a été exécutée en tâche de fond avec une notification à la fin, sans boucle de surveillance, parce que sa durée pouvait dépasser la limite de 10 min d'une commande au premier plan.

```
INFO  [alembic.runtime.migration] Running upgrade 5d2f8c1a7b30 -> 40038a21e22f, ... (revue S9)
======== 546 passed, 1735 deselected, 32 warnings in 323.38s (0:05:23) =========
 Network interview-rag-trainer-test_default Removed
exit=0
```

Les 32 avertissements viennent de Docling (`DeprecationWarning`, 21 dans `test_office_extraction.py` et `test_office_parsers_live.py`) et de `PytestReturnNotNoneWarning` sur `test_extraction_failures.py::test_parser` (défaut D1). La stack de test a été détruite par la cible (`test-integration-down`).

## 3. Alembic sur une base jetable — OK

`docker compose -f compose.test.yaml up -d --wait` (postgres_test, qdrant_test, rabbitmq_test, worker_test : `Healthy`), puis, dans `backend/`, avec les variables de `INTEGRATION_ENV` (PostgreSQL `localhost:5441`, base `irt_test`) :

| Commande | Sortie | rc |
|---|---|---|
| `uv run alembic upgrade head` | 10 révisions appliquées, de `-> 7c1a0e2b9d41` jusqu'à `5d2f8c1a7b30 -> 40038a21e22f` | 0 |
| `uv run alembic current` | `40038a21e22f (head)` | 0 |
| `uv run alembic downgrade base` | 10 retours en arrière, de `40038a21e22f -> 5d2f8c1a7b30` jusqu'à `7c1a0e2b9d41 -> , initial empty schema` | 0 |
| `uv run alembic current` | (vide : aucune révision) | 0 |
| `uv run alembic upgrade head` | les 10 révisions réappliquées | 0 |
| `uv run alembic current` | `40038a21e22f (head)` | 0 |
| `uv run alembic check` | `No new upgrade operations detected.` | 0 |

Le downgrade est allé **jusqu'à `base`**, pas seulement jusqu'à la première révision KB. Fin : `docker compose -f compose.test.yaml down -v`, sans aucun conteneur `interview-rag-trainer-test` restant.

## 4. Stack de dev Compose — OK

Commande : `docker compose up -d --build --wait` (rc 0, aucun volume supprimé).

**Migrations** : appliquées par le service `migrate` (`command: alembic upgrade head`, `compose.yaml:79-81`), dont `kb_provision`, `api`, `worker` et `worker_ingestion` attendent la fin (`service_completed_successfully`). Journal de `migrate` pendant cette validation :

```
Running upgrade 5d2f8c1a7b30 -> 40038a21e22f, knowledge_base search_indexes : au plus un index BUILDING par corpus (revue S9)
```

La base de dev était donc à `5d2f8c1a7b30` avant le démarrage. Vérification : `docker compose run --rm -T migrate alembic current` répond `40038a21e22f (head)`. `kb_provision` : `index actif de Knowledge Base : 76ebad1e-b26d-4a00-a3cc-deb6471a786e`.

**Santé** (`docker inspect`, après `up`) :

| Service | État |
|---|---|
| api | healthy |
| worker | healthy |
| worker_ingestion | healthy |
| postgres | healthy |
| qdrant | healthy |
| rabbitmq | healthy |
| scheduler | Up, **aucun healthcheck** (`health=none`) ; `--wait` l'affiche pourtant `Healthy` (défaut D3) |
| migrate, kb_provision | Exited (0) |

`curl localhost:8010/health/ready` répond `{"status":"ready","checks":{"postgres":{"status":"ok",...},"qdrant":{"status":"ok",...},"rabbitmq":{"status":"ok",...}}}`.

**Scheduler** (`docker compose logs scheduler`, décompte en fin de validation) : 265 `Sending due task knowledge_base.work.dispatch`, 17 `knowledge_base.work.recover`, 1 `knowledge_base.work.reconcile`. Exemples :

```
[11:25:15,161] Scheduler: Sending due task knowledge_base.work.dispatch (knowledge_base.work.dispatch)
[11:25:43,152] Scheduler: Sending due task knowledge_base.work.recover (knowledge_base.work.recover)
[11:30:13,153] Scheduler: Sending due task knowledge_base.work.reconcile (knowledge_base.work.reconcile)
```

**Workers** : `worker` a reçu les tâches `dispatch` et `recover` (`Task knowledge_base.work.dispatch[...] received`, `Task knowledge_base.work.recover[...] received`) ; `worker_ingestion` a reçu les `knowledge_base.work.execute[...]` des imports, et le cycle de réconciliation y a laissé `[11:30:13,292] réconciliation : aucun écart (1 index, 0.05 s)`.

## 5. Imports de bout en bout par l'API REST — OK

Routes lues dans `knowledge_base/adapters/controller/rest/` : `POST /documents`, `POST /documents/{id}/versions`, `DELETE /documents/{id}`, `POST /search`, `GET /failures`, `POST /failures/{item_id}/retry`. Aucune route ne lit l'état d'un document : on le suit donc par `psql` (observation D4).

La base de dev contenait déjà 17 versions, dont `scanned.pdf`, `cours_git_fr.docx` et les Markdown de `retrieval/`. Pour éviter la réponse 200 de la déduplication, j'ai choisi des fixtures **jamais importées** (vérifié par `select filename from knowledge_base.document_versions`). Chaque import porte le formulaire `kind=COURSE`, `language=fr`, `topics=validation` et un `Idempotency-Key`.

| Format | Fichier | Réponse | Version | Indexation |
|---|---|---|---|---|
| MD | `office/cours_concurrence_long_fr.md` | 202, document `caf39b94…` | PUBLISHED | COMPLETED, 6 points |
| TXT | `notes_python.txt` | 202, document `a9d6d40e…` | PUBLISHED | COMPLETED, 1 point |
| PDF | `office/qa_pytest_fr.pdf` | 202, document `a7e6191f…` | PUBLISHED | COMPLETED, 6 points |
| DOCX | `office/qa_pytest_fr.docx` | 202, document `f2ed5969…` | PUBLISHED | COMPLETED, 6 points |
| PDF scanné | copie de `office/scanned.pdf` avec un commentaire PDF ajouté en fin de fichier (`scanned_validation.pdf`, empreinte différente) | 202, document `a2e159e0…` | **FAILED, `OCR_REQUIRED`** | aucune |

Imports à 11:25:40 UTC ; quatre publications entre 11:25:50 et 11:25:53. Journal de `worker_ingestion` : `qa_pytest_fr.pdf : 2 page(s) converties par Docling en 1.8 s` ; `work item ab28bc99… (extract_version) tentative 1/5 : OCR_REQUIRED → fail`.

**Recherche** (`POST /search`, `limit` 5, index `76ebad1e`, `complete: true`). Avec le filtre `topics=["validation"]` :

| Document | Requête | Rang du document |
|---|---|---|
| MD | « Comment exécuter du code bloquant depuis une application asyncio ? » | 1 (score 0,886) |
| TXT | « Les listes en compréhension sont-elles plus rapides qu'une boucle for avec append ? » | 1 (0,835) |
| PDF | « Comment rejouer un test avec plusieurs jeux de valeurs ? » | 1 (0,872, `page_start` 1) |
| DOCX | même requête | 2 (0,872, `page_start` null) |

Le filtre `document_ids` sur chacun rend 3, 1, 5 et 5 passages, et le premier passage cite bien le fichier attendu.

**Retrait** du document TXT :

```
DELETE /api/v1/knowledge-base/documents/a9d6d40e-…  → 202 {"status":"WITHDRAWN","visibility_revision":2}
POST /search (même requête, sans filtre), immédiatement → notes_python.txt absent des 5 résultats
POST /search {"document_ids":["a9d6d40e-…"]}          → 200 {"complete":false,"passages":[]}
DELETE (rejeu)                                          → 409 {"code":"DOCUMENT_WITHDRAWN",…}
```

Ensuite, `purge_document` est `SUCCEEDED`, `documents.purged_at` vaut `2026-11-19 11:26:29` et Qdrant compte **0** point avec `document_id = a9d6d40e…` (le même comptage rend 6 pour le document MD, ce qui confirme que le filtre fonctionne). Après les deux migrations du point 8, le filtre `document_ids` du TXT rend toujours 0 passage sur le nouvel index `8aaafbf0`.

**`GET /failures?limit=20`** :

```
extract_version OCR_REQUIRED replayable=False attempts=1 2026-11-19T11:25:49 a2e159e0-… scanned_validation.pdf : aucune couche texte sur ses 1 page(s) (document scanné ?…
extract_version OCR_REQUIRED replayable=False attempts=1 2026-11-18T17:16:24 42a568a5-… scanned.pdf : aucune couche texte …
```

## 6. Fiabilité — OK

### Tests d'intégration couvrant chaque garantie

Tous font partie des 546 tests verts du point 2. Les noms viennent d'un `grep -n "async def test_"` sur `backend/tests/integration/knowledge_base/`.

| Garantie | Tests (fichier :: nom exact) |
|---|---|
| **Redélivrance** (message RabbitMQ rejoué ou perdu) | `test_celery_flow.py::test_the_same_message_delivered_twice_produces_a_single_effect` ; `test_celery_flow.py::test_a_message_acknowledged_then_lost_before_the_claim_is_redispatched` ; `test_consumer_timeout.py::test_a_prefetched_message_outliving_consumer_timeout_causes_no_double_effect` (scénario 14 de S0, vrai worker prefork, `consumer_timeout` de 5 s) ; `test_dispatch.py::test_a_notification_that_nobody_consumes_is_published_again` ; `test_dispatch.py::test_max_in_flight_ignores_stale_notifications` |
| **Lease expiré** | `test_celery_flow.py::test_the_recover_task_requeues_an_expired_lease_and_the_cycle_completes` ; `test_work_lifecycle.py::test_recover_puts_an_expired_item_back_to_pending_with_backoff` ; `test_work_lifecycle.py::test_recover_fails_an_expired_item_without_attempts_left` ; `test_work_lifecycle.py::test_recover_leaves_a_live_lease_alone` ; `test_dispatch.py::test_recover_expired_leases_applies_the_backoff_from_the_settings` ; `test_execution.py::test_a_worker_killed_after_the_claim_is_recovered_and_reclaimed` ; `test_execution.py::test_a_worker_killed_on_the_last_attempt_leaves_the_item_failed` ; `test_execution.py::test_a_heartbeat_extends_the_lease_of_the_running_attempt` |
| **Fencing** | `test_execution.py::test_a_superseded_attempt_cannot_finalize_after_its_lease_was_reassigned` ; `test_execution.py::test_a_heartbeat_after_cancellation_stops_the_attempt_with_stale_attempt` ; `test_execution.py::test_cancelling_during_execution_rejects_the_finalization` ; `test_work_lifecycle.py::test_heartbeat_with_a_stale_token_returns_false` ; `test_work_queue_port.py::test_cancel_a_pending_item_cancels_it_and_bumps_the_fencing_token` ; `test_extraction_fencing.py::test_a_stale_attempt_writes_no_unit_and_no_chunk` |
| **Idempotence des effets** | `test_execution.py::test_executing_a_finished_item_again_is_skipped_without_a_second_effect` ; `test_extraction_rerun.py::test_a_re_extraction_after_a_crash_yields_the_same_identifiers` ; `test_extraction_rerun.py::test_executing_a_finished_item_again_duplicates_nothing` ; `test_indexing_idempotence.py::test_a_crash_between_the_upserts_and_the_finalization_leaves_points_only`, `::test_the_retry_after_a_crash_completes_without_any_duplicate`, `::test_two_crashes_then_a_success_give_the_same_points_and_manifest`, `::test_a_zombie_rewrite_after_the_finalization_leaves_one_point_per_chunk` ; `test_qdrant_vector_index.py::test_set_visibility_is_idempotent` ; `test_document_purger.py::test_a_replayed_purge_keeps_the_first_purge_date` ; `test_import_api.py::test_the_same_key_and_request_replay_the_same_response` |
| **Concurrence** | `test_execution.py::test_the_work_is_not_executed_when_the_claim_is_lost_to_another_worker` ; `test_import_atomicity.py::test_concurrent_version_imports_keep_a_single_live_version`, `::test_two_concurrent_imports_of_the_same_new_file_yield_one_document`, `::test_the_same_key_sent_twice_in_parallel_on_versions_answers_twice_202` ; `test_knowledge_corpus_repository.py::test_two_concurrent_creations_leave_one_corpus_with_one_index`, `::test_concurrent_provisioning_by_the_handler_converges_on_one_index` ; `test_s8_retry_ingestion.py::test_two_simultaneous_retries_reopen_the_item_once` ; `test_s8_failed_work.py::test_an_adoption_holding_the_item_makes_a_concurrent_retry_wait`, `::test_a_retry_holding_the_item_makes_a_concurrent_adoption_give_up` ; `test_s9_concurrency.py::test_two_concurrent_starts_towards_different_targets_one_succeeds` (et les 4 autres tests du fichier) ; `test_s7_concurrent_versions.py::test_searches_during_a_republication_never_mix_two_versions` ; `test_prefork.py::test_two_tasks_in_a_prefork_child_reuse_the_same_loop_and_engine` |

### Démonstration sur la stack de dev : redémarrage pendant une ingestion

Fichier : un PDF de 100 pages construit pour l'occasion, hors du dépôt, en concaténant 20 fois `qa_pytest_fr.pdf` et `cours_concurrence_long_fr.pdf`. Chronologie (UTC) :

| Heure | Fait observé |
|---|---|
| 11:26:43 | `Task knowledge_base.work.execute[813df0a5…] received` ; `Processing document long_restart.pdf` (Docling) ; item `extract_version` `RUNNING`, tentative 1, jeton 1, lease jusqu'à 11:31:43 |
| 11:26:52 | `docker compose restart worker_ingestion` (`Warm shutdown`), pendant la conversion |
| 11:26:55 | `celery@… ready.` (nouveau processus) ; l'item reste `RUNNING`, sous le lease de la tentative interrompue |
| 11:32:13 | `Task knowledge_base.work.recover[9ebf867d…] succeeded … : 1` (un lease expiré récupéré) |
| 11:33:09 | `long_restart.pdf : 100 page(s) converties par Docling en 50.6 s` |
| 11:33:12 | `extract_version` `SUCCEEDED`, `attempts=2`, `fencing_token=2`, `last_error_code=LEASE_EXPIRED` ; `180 unités, 220 chunks` |
| 11:33:51 | indexation `COMPLETED`, 220 points ; version `PUBLISHED` |
| 11:33:53 | `sync_visibility` `SUCCEEDED` |

La reprise attend l'expiration du lease (`KB_WORK_LEASE_TTL_SECONDS=300`) puis le passage suivant de `recover` (30 s) : c'est le comportement prévu par l'ADR 0004. Il n'y a eu aucun doublon : 220 points pour 220 chunks.

## 7. Qualité de recherche — OK

Les tests de retrieval font partie du point 2. Ils ont écrit leurs mesures à 12:22 heure locale dans `backend/.cache/retrieval/`. Valeurs mesurées pendant cette exécution :

**S5** (`retrieval_report.json` : 42 requêtes, 12 documents, e5-base) comparé aux seuils de `backend/tests/fixtures/knowledge_base/retrieval/retrieval_thresholds.json` :

| Métrique | Mesuré | Minimum asserté |
|---|---|---|
| recall_at_1 | 0,810 | — |
| recall_at_5 | 0,881 | 0,83 |
| mrr | 0,853 | 0,80 |
| unit_integrity | 1,000 | 0,94 |
| cross_language_recall_at_5 | 0,750 | 0,66 |
| same_language_recall_at_5 | 0,933 | — |
| recall_at_5_with_filters / sans filtre | 0,975 / 0,875 | 0,92 / — |

Ces valeurs sont identiques à la mesure de référence du 2026-11-18 inscrite dans le fichier de seuils.

**S6 office** (`retrieval_report_office.json`, 42 requêtes) : Recall@1 0,810, Recall@5 0,905, MRR 0,857, intégrité 1,000, inter-langues 0,750, avec filtres 1,000. Le test asserte `recall_at_5 >= minimum` (`test_retrieval_evaluation_office.py:282-286`).

**S6 unités longues** (`retrieval_report_long_units.json`, 10 requêtes ; budget 1024 tokens, celui de la production) : Recall@5 0,900, MRR 0,833, unité longue dans les 5 premiers 1,000, intégrité des passages tronqués 0,875. Seuils de `office/long_units_thresholds.json` : `long_unit_found_at_5` ≥ 0,87, `recall_at_5` ≥ 0,8, `truncated_unit_integrity` ≥ 0,75.

**Benchmark** ([knowledge-base-embedding-benchmark.md](knowledge-base-embedding-benchmark.md)), colonne **e5-base** : poids 1 061 Mio ; RSS après chargement 980 Mio, pic après encodage 2 584 Mio ; encodage d'un passage 84,0 ms (médiane, lots de 32) ; requête 13,4 ms en médiane, 17,1 ms au p95. S5 : Recall@1 0,810, Recall@5 0,881, MRR 0,853, intégrité 1,000, inter-langues 0,750, même langue 0,933, avec et sans filtres 0,975 / 0,875. S6 office : Recall@5 0,905, MRR 0,857. S6 unités longues : Recall@5 0,900, MRR 0,833, intégrité tronquée 0,875, identifiants de code Recall@5 1,000, négatifs proches Recall@1 0,800. S9 identifiants de code (12 requêtes) : Recall@1 0,833, Recall@5 1,000 (hybride : 1,000 / 1,000) ; S9 négatifs proches Recall@1 1,000. Les valeurs S5 et S6 du benchmark sont celles que l'intégration vient de remesurer.

## 8. Blue/green sur la stack de dev — OK

e5-small était déjà dans `models_cache` (`ls /var/cache/interview-rag-trainer/models` dans `api` : `models--intfloat--multilingual-e5-small`) : aucun téléchargement n'a été nécessaire. Les recherches tournaient en boucle par REST (4 requêtes alternées, `limit` 5) pendant chaque bascule.

**Aller (e5-base → e5-small)**

```
make kb-migration-start target=multilingual-e5-small
msg=migration démarrée : index c1250977-5d62-4858-a801-16bfb22e039b BUILDING (profil intfloat/multilingual-e5-small@614241f6…, politique kb-chunking-v1+xlmr-614241f6), backfill de 19 version(s) dont 4 à rechunker
```

Backfill terminé à 11:36:13, soit environ 2 min après le démarrage de 11:34:12 : `index_version` SUCCEEDED 19, `rechunk_version` SUCCEEDED 4, indexations COMPLETED 19. Puis :

```
make kb-migration-activate index=c1250977-…
msg=index c1250977-… activé (19 version(s) publiée(s) couvertes) ; ancien index 76ebad1e-… RETIRED, collection supprimée dans 0:05:00
```

Boucle : **2 459 requêtes, 0 erreur, 0 réponse vide, 1 bascule** (2 139 servies par `76ebad1e`, puis 320 par `c1250977`), sur 224 s.

**Abandon** (migration vers e5-base démarrée puis abandonnée)

```
make kb-migration-start target=multilingual-e5-base
msg=migration démarrée : index fe444026-… BUILDING (…e5-base…), backfill de 19 version(s) dont 0 à rechunker
(3 s plus tard : index_version PENDING 16, SUCCEEDED 3)
make kb-migration-abandon index=fe444026-…
msg=migration vers l'index fe444026-… abandonnée : index RETIRED, 16 indexation(s) annulée(s) sur 19, collection supprimée dans 0:05:00
```

État constaté : index `fe444026` RETIRED ; `index_version` CANCELLED 16 et SUCCEEDED 3 ; indexations CANCELLED 16 et COMPLETED 3 ; `drop_index_collection` PENDING. L'index actif (`c1250977`) a continué de servir la recherche (200). Un second `abandon` sur le même index est refusé : `InvalidIndexTransitionError: index fe444026-… : retrait interdit depuis RETIRED`, et `make` sort en erreur, avec une trace Python brute (défaut D2).

**Retour (e5-small → e5-base)**

```
make kb-migration-start target=multilingual-e5-base
msg=migration démarrée : index 8aaafbf0-0b1b-4102-a6c6-e1e6bcf1da47 BUILDING (…e5-base…), backfill de 19 version(s) dont 0 à rechunker
(backfill : 77 s, index_version SUCCEEDED 19, indexations COMPLETED 19)
make kb-migration-activate index=8aaafbf0-…
msg=index 8aaafbf0-… activé (19 version(s) publiée(s) couvertes) ; ancien index c1250977-… RETIRED, collection supprimée dans 0:05:00
```

Boucle : **1 096 requêtes, 0 erreur, 0 réponse vide, 1 bascule** (824 servies par `c1250977`, puis 272 par `8aaafbf0`), sur 105 s.

**État final** : `select … from knowledge_base.search_indexes where status <> 'RETIRED'` rend une seule ligne, `8aaafbf0-0b1b-4102-a6c6-e1e6bcf1da47 | ACTIVE | intfloat/multilingual-e5-base`. Les recherches filtrées sur les documents MD, PDF et DOCX du point 5 rendent 3, 5 et 5 passages sur cet index. Les trois `drop_index_collection` (pour `76ebad1e`, `fe444026` et `c1250977`) sont passés `SUCCEEDED` à 11:42:31, 11:43:11 et 11:44:45, soit 300 s après chaque bascule ou abandon. Ensuite, `curl localhost:6333/collections` ne liste plus que `kb_index_8aaafbf00b1b4102a6c6e1e6bcf1da47`. Les services sont tous `Up`, et `healthy` pour ceux qui ont un healthcheck.

## 9. Frontières entre BC — OK

Dans `backend/src` :

```
$ grep -rnE "^\s*(from|import)\s+knowledge_base" --include='*.py' . | grep -vE "^(\./)?knowledge_base/"
runtime/worker_factory.py:30:import knowledge_base
runtime/index_migration.py:36:import knowledge_base
runtime/provision.py:17:import knowledge_base
runtime/app.py:18:import knowledge_base
```

Hors du contexte, seul `runtime` importe Knowledge Base, et **uniquement sa racine**. Un filtre complémentaire, qui exclut `import knowledge_base` et `knowledge_base.contracts`, ne rend aucune ligne.

```
$ grep -rn "knowledge_base" --include='*.py' interview_training assessment shared
(aucune ligne)
```

`interview_training/` et `assessment/` ne contiennent aujourd'hui qu'un `README.md`. Dans l'autre sens, `grep -rnE "^\s*(from|import)\s+(interview_training|assessment|runtime)" knowledge_base shared` ne rend rien, et aucun import dynamique (`importlib`, `__import__`) n'existe dans `src/`.

Imports de `knowledge_base/contracts` (tous les fichiers) :

```
3 __future__   2 dataclasses   1 enum   1 typing   2 uuid
+ knowledge_base.contracts.{providers, providers.knowledge_search, result, result.knowledge_query, result.knowledge_search_result}
```

Ce sont la bibliothèque standard et la façade elle-même, ce qu'assertent aussi `test_contracts_only_import_the_standard_library_and_themselves` et la règle `contexts_never_import_each_other_internals` (point 1).

---

## Défauts trouvés

Aucun n'est bloquant. Aucun n'a été corrigé (consigne : pas de modification du code).

| # | Gravité | Fichier:ligne | Constat | Reproduction |
|---|---|---|---|---|
| D1 | mineur (tests) | `backend/tests/integration/knowledge_base/extraction_support.py:57`, importé dans `test_extraction_failures.py:21` | La fabrique `test_parser()`, dont le nom commence par `test_`, est importée dans un module de test : pytest la collecte comme un test et émet `PytestReturnNotNoneWarning: Test functions should return None, but tests/integration/knowledge_base/test_extraction_failures.py::test_parser returned <class '…RoutingDocumentParser'>`. Le test fantôme passe toujours et gonfle le compte. Une future version de pytest pourrait transformer cet avertissement en erreur. | `make test-integration`, section `warnings summary` |
| D2 | mineur (exploitation) | `backend/src/runtime/index_migration.py:99-108` (`main`) | Les refus du domaine (`InvalidIndexTransitionError`, et sans doute de même `IncompleteCoverageError` ou `MigrationInProgressError`, non essayés) remontent en trace Python brute. Le refus est correct et sans effet (code de sortie non nul), mais la sortie n'est pas un message d'opérateur. | `make kb-migration-abandon index=<index RETIRED>` |
| D3 | mineur (exploitation) | `compose.yaml:199-209` (service `scheduler`) | Aucun `healthcheck`. `docker compose up --wait` l'affiche `Healthy` dès qu'il est démarré, alors que `docker inspect` donne `health=none` : une panne de beat (aucun `dispatch` ni `recover`) ne se verrait pas dans l'état Compose. | `docker inspect --format '{{.State.Health}}' interview-rag-trainer-scheduler-1` |
| D4 | observation (API) | `knowledge_base/adapters/controller/rest/` | Aucune route ne lit l'état d'un document ou d'une version (`RECEIVED`, `PUBLISHED`, `FAILED`) : un client REST ne sait pas quand son import est publié, sauf par `/failures` en cas d'échec. Le suivi de cette validation a dû passer par `psql`. À décider (nouvelle route ou hors périmètre de la v1). | liste des routes ci-dessus |
| D5 | observation (journaux) | `extract_version_handler.py:151-155` et `execution.py:121` | Un refus terminal attendu (`OCR_REQUIRED`) est journalisé en WARNING avec deux traces complètes chaînées, ce qui produit du bruit pour un cas métier normal. | import de `scanned.pdf` (copie modifiée), `docker compose logs worker_ingestion` |

## Tableau Problème / Avant / Après

| Problème | Avant (faits établis) | Après |
|---|---|---|
| Schéma de la base de dev en retard | Journal de `migrate` : `Running upgrade 5d2f8c1a7b30 -> 40038a21e22f` au démarrage du 2026-11-19 11:25 UTC, donc la base était à `5d2f8c1a7b30` | `alembic current` = `40038a21e22f (head)` |
| Absence de preuve globale S0–S9 sur un même commit | Validations faites slice par slice ; aucun rapport sur `91c5b8c` | Ce rapport : points 1 à 9 exécutés sur `91c5b8c`, tous OK |
| Index actif de la stack de dev | `76ebad1e-b26d-4a00-a3cc-deb6471a786e`, e5-base, ACTIVE | `8aaafbf0-0b1b-4102-a6c6-e1e6bcf1da47`, e5-base, ACTIVE, après l'aller-retour (0 erreur sur 3 555 recherches) |
| Fixture de test collectée comme test (D1) | Avertissement `PytestReturnNotNoneWarning` sur `test_extraction_failures.py::test_parser` | Corrigé après la validation par l'orchestrateur (`test_parser.__test__ = False`) ; plus collectée |
| Refus de la CLI de migration en trace brute (D2) | `InvalidIndexTransitionError` affichée en trace Python, `make` en erreur | Corrigé après la validation : base publique `knowledge_base.IndexOperationRefusedError`, une ligne `refusé : …` sur stderr, code 1, sans trace (voir « Suites ») |
| Scheduler sans healthcheck (D3) | `health=none` ; `--wait` affiche `Healthy` | Corrigé après la validation : healthcheck sur l'âge du fichier de schedule de beat, `health=healthy` (voir « Suites ») |
| Pas de lecture d'état d'un document par REST (D4) | Seules routes : imports, retrait, recherche, échecs | Inchangé : à décider |
| Refus terminal journalisé avec trace (D5) | Deux traces pour `OCR_REQUIRED` | Corrigé après la validation : une ligne WARNING avec le code, sans trace ; ERROR tracé pour une erreur inattendue (voir « Suites ») |

## Suites (corrections postérieures à la validation)

Corrections apportées après ce rapport, sur la même branche ; les constats
ci-dessus restent ceux du commit `91c5b8c`.

- **D2** : `IndexOperationRefusedError` (`domain/exceptions/corpus_errors.py`,
  réexportée par la racine `knowledge_base`) est la base de tous les refus de
  `start`, `activate` et `abandon` ; `runtime.index_migration.main` l'attrape
  seule. `make kb-migration-abandon index=00000000-0000-0000-0000-000000000000`
  sur la stack de dev : `refusé : index 00000000-… : inconnu du corpus`, puis
  `make: *** [kb-migration-abandon] Error 1`, sans trace.
- **D3** : healthcheck du service `scheduler` : le plus récent des fichiers
  `/tmp/celerybeat-schedule*` doit avoir moins de 300 s. Constaté dans le
  conteneur : beat réécrit `celerybeat-schedule-wal` toutes les 180 s
  (`Scheduler.sync_every`, Celery 5.7.0 ; dbm `sqlite3` en WAL sous Python
  3.13), le fichier principal n'est écrit qu'au démarrage. Après `docker
  compose up -d --build --wait`, `docker inspect` donne `healthy`. Dans un
  conteneur éphémère : sans fichier, code 1 ; fichiers de 400 s, code 1 ;
  `-wal` frais, code 0.
- **D5** : `execution.log_failed_attempt` : une `TerminalWorkError` donne une
  ligne WARNING avec son code et son message, sans trace ; une erreur
  inattendue et l'erreur d'un hook d'échec sont des ERROR tracés.

## Corrections post-review

Review indépendante (Codex) du commit `28e29fc`, 2026-11-23 : un constat
bloquant et trois importants
([rapport](knowledge-base-independent-review-report.md)). Les quatre ont été
reproduits par un test rouge avant correction, puis corrigés sur la même
branche. Précisions datées : [conception](knowledge-base.md) §8, §12, §15 et
O16 ; [README du contexte](../../../backend/src/knowledge_base/README.md) ;
suivi de l'[ADR 0012](../../decisions/0012-original-document-storage-and-purge.md).

| # | Constat | Reproduit | Correction | Tests |
|---|---|---|---|---|
| 1 | Bloquant : deux versions d'un document dans une réponse | Oui : versions 3 et 4 rendues ensemble (doubles) ; N et N+1 ensemble sur PostgreSQL et Qdrant réels | Chaque lecture SQL relit les unités déjà retenues avec les nouveaux candidats ; lecture en `REPEATABLE READ` (un instantané) ; une relecture qui écarte une unité est un rejet remplacé par la passe suivante, sinon `complete=false` | `test_search_knowledge_snapshot` (3), `test_search_version_snapshot` (intégration, barrière entre les passes) |
| 2 | Question longue hors budget | Oui : 5 403 tokens rendus pour un budget de 1 024 (question de 5 001 tokens) | Question réservée avant la fenêtre (moitié du budget au plus, tronquée aux mots, Proposed) ; texte final compté et tronqué au budget s'il dépasse | `test_passage_assembler` (5 nouveaux, 3 ajustés) |
| 3 | Upload chunked lu en entier avant le 413 | Oui : 200 455 octets lus pour une limite de 1 000 | `receive` ASGI enveloppé dans la classe de route : lecture interrompue au plafond global | `test_document_routes_http` (3 nouveaux : arrêt de lecture compté) |
| 4 | Partie DOCX reliée hors comptage | Oui : partie binaire reliée de 100 Mio + 1 acceptée par la limite de production ; python-docx la charge | Somme des `file_size` de toute l'archive, `MAX_DOCX_UNCOMPRESSED_BYTES` = 100 Mio (Proposed) | `test_office_parser_failures` (7 nouveaux, 2 réécrits) ; fixtures DOCX acceptées |

Pourquoi le test S7 de non-mélange ne voyait pas le constat 1 : ses
recherches tournent librement autour de la publication, sans barrière entre
deux passes d'une même recherche, et, tant que N est publiée, rien n'est
rejeté, donc aucun complément n'a lieu. Il reste vert avec la correction, et
il reste vert aussi avec la mutation qui la retire : il ne protège pas ce cas.

Mutations, une par constat (correction retirée, puis restaurée) :

| # | Mutation | Tuée par |
|---|---|---|
| 1 | Relecture des seuls nouveaux candidats (code d'avant) | 3 tests unitaires ; `make test-integration` : `1 failed, 548 passed` (`test_search_version_snapshot`, version N en plus de N+1) |
| 2 | Question non réservée, pas de contrôle final | 4 tests unitaires |
| 3 | `receive` non enveloppé | 2 tests unitaires |
| 4 | Somme limitée à `word/*.xml` | 4 tests unitaires |

Validation après correction : `uv run ruff check . && uv run ruff format
--check . && uv run mypy && uv run pytest -q` → 1 780 tests passent ;
`make test-integration` → 549 tests passent (548 + 1 nouveau), 31
avertissements.

Non mesurés : la règle de la moitié du budget pour la question (aucune
question du jeu de référence n'en approche) ; la RSS de Docling sur un DOCX
proche de 100 Mio décompressés ; le coût du répertoire central d'une archive
à très nombreuses entrées.

## Non exécuté

- **Frontend** (`make frontend-lint frontend-test`) : hors du périmètre demandé (backend Knowledge Base).
- **Rejeu d'un échec par `POST /failures/{id}/retry` sur la stack de dev** : non demandé. Le refus est couvert par les tests d'intégration (`test_s8_retry_ingestion.py::test_a_deterministic_failure_cannot_be_replayed`, entre autres), mais n'a pas été rejoué à la main.
- **PDF chiffré ou corrompu (`INVALID_DOCUMENT`) sur la stack de dev** : seul le PDF scanné était demandé. Les deux cas sont couverts par les tests d'intégration du point 2.
- **`make smoke-pdf-image`** : non demandé. La conversion Docling dans l'image est démontrée de fait par les imports PDF du point 5 et par le PDF de 100 pages du point 6.

## Annexe — artefacts hors dépôt

Artefacts créés pour la validation, dans le répertoire temporaire de la session et non versionnés : `scanned_validation.pdf` (copie de `scanned.pdf` avec un commentaire ajouté en fin de fichier), `long_restart.pdf` (100 pages), le script de recherche en boucle et ses rapports JSON. La base de dev garde, en plus de son contenu antérieur, les documents de validation (topics `validation` et `validation-restart`) et le tombstone du TXT retiré.
