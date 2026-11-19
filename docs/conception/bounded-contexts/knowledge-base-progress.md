# Knowledge Base — Suivi de l'objectif global

> **Objectif :** *Implement Knowledge Base — Document Ingestion & Semantic Retrieval*
> (S0 à S9, une seule branche `feature/knowledge-base-v1`, une seule PR finale).
>
> Ce fichier remplace la commande `/goal`, absente de l'environnement Claude
> Code utilisé (constat du 2026-11-17). Il est mis à jour à la fin de chaque
> slice. Référence : [feuille de route](knowledge-base-roadmap.md).

## Statut global

**Pending Independent Review** (2026-11-19) — S0–S9 implémentées et revues
par slice, validation globale OK sur les 9 points
([rapport](knowledge-base-validation.md)), documentation synchronisée,
PR globale ouverte sans merge. **La review indépendante n'a pas été
effectuée** : Codex n'est pas exécutable dans l'environnement (aucun binaire
`codex`, aucune application ; constats du 2026-11-17 et du 2026-11-19). Le
dossier et le prompt sont prêts :
[knowledge-base-independent-review.md](knowledge-base-independent-review.md).
Clôture : review faite, constats bloquants corrigés et retestés.

Décisions en attente de l'utilisateur : ADR 0013 (profil définitif, Proposed)
et barrière de l'ADR 0011 (point 5, Proposed).

## Jalons

| Slice | Objectif | Dépend de | Statut | Commit(s) | Tests exécutés |
|---|---|---|---|---|---|
| S0 | Infrastructure de traitement durable | — | **Terminée** | voir journal | Ruff, mypy (68 fichiers), 130 unit./archi., 109 intégration, Compose (9 services) |
| S1 | `KnowledgeDocument` et `DocumentVersion` | S0 | **Terminée** | voir journal | Ruff, mypy (86 fichiers), 233 unit./archi., 8/8 mutations détectées |
| S2 | Import Markdown/TXT, stockage, dédoublonnage | S0, S1 | **Terminée** | voir journal | Ruff, mypy (125 fichiers), 309 unit./archi., 162 intégration, import réel sur la stack de dev (202 puis 200) |
| S3 | Docling Markdown, `KnowledgeUnit`, chunking | S2 | **Terminée** | voir journal | Ruff, mypy (180 fichiers), 529 unit./archi., 192 intégration, extraction réelle sur la stack de dev (3 unités QA, 3 chunks) |
| S4 | Embeddings, Qdrant, index initial, publication | S3 | **Terminée** | voir journal | Ruff, mypy (252 fichiers), 759 unit./archi., 273 intégration (18 `model`), 3/3 mutations, import réel jusqu'à PUBLISHED (3 points visibles) |
| S5 | Recherche sémantique, provenance, évaluation | S4 | **Terminée** | voir journal | Ruff, mypy (299 fichiers), 1 115 unit./archi., 321 intégration (dont `retrieval`), 4 mutations tuées, recherche réelle sur la stack de dev (citation complète) |
| S6 | PDF et DOCX avec Docling | S3, S5 | **Terminée** | voir journal | Ruff, mypy (316 fichiers), 1 196 unit./archi., 364 intégration, 3/3 mutations, import réel PDF et DOCX jusqu'à PUBLISHED, recherche avec pages |
| S7 | Versions concurrentes, retrait, purge | S4, S5 | **Terminée** | voir journal | Ruff, mypy (356 fichiers), 1 305 unit./archi., 392 intégration, tests de concurrence S7 verts 13/13, 4 mutations, retrait + purge réels sur la stack de dev |
| S8 | Réconciliation et gestion des erreurs | S7 | **Terminée** | voir journal | Ruff, mypy (419 fichiers), 1 567 unit./archi., 484 intégration, tests S8 3/3 stables, 7 mutations, écart réel corrigé en un cycle sur la stack de dev, échec rejoué par l'API |
| S9 | Benchmark embeddings, blue/green | S5, S8 | **Terminée** | voir journal | Ruff, mypy (474 fichiers), 1 735 unit./archi., 546 intégration (stack neuve, après revue), 5 + 4 mutations, 2 migrations blue/green sur la stack de dev (12 657 recherches, 0 erreur) |

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

- 2026-11-18 — S6 terminée : tests rouges (sorties Docling réelles
  enregistrées) → implémentation → revalidation : 1 196 unitaires,
  364 intégration. PDF 300 pages : 15 min 13 s jusqu'à PUBLISHED, pic RSS
  2 645 Mio ; image `production` 480 → 558 Mo ; base de dev reconstruite.
- 2026-11-18 — S7 terminée : tests rouges (implémentation jetable hors
  dépôt, 18 mutations) → implémentation → revalidation : 1 305 unitaires,
  392 intégration. Défaut préexistant corrigé : recherche en 500 pendant une
  suppression de points Qdrant.
- 2026-11-18 — S8 terminée : tests rouges (implémentation jetable, 24
  mutations) → implémentation → revalidation : 1 567 unitaires,
  484 intégration. O10, O14, O15, O16 tranchés ; catalogue unique des codes.
- 2026-11-19 — S9 terminée : benchmark de 4 profils (e5-small, e5-base,
  bge-m3, Qwen3-Embedding-0.6B) → ADR 0013 **Proposed** (conserver e5-base) ;
  migration blue/green (`StartIndexMigration`, `ActivateSearchIndex`,
  `drop_index_collection`, `rechunk_version`, `EmbedderProvider`) démontrée
  aller-retour sur la stack de dev, 0 erreur de recherche. Revalidation :
  1 708 unitaires, 527 intégration. Incident : l'agent a attendu ~13 h une
  boucle de surveillance qui ne pouvait aboutir (bge-m3 terminé, ligne de fin
  hors de la fenêtre lue) ; relancé, mesure refaite au premier plan.

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

- S6 — limite de 300 pages appliquée au parsing (FAILED `TOO_MANY_PAGES`, non rejouable), pas à l'import ; pas de limite de pages pour DOCX ; volume de texte DOCX non borné → O16 [Open] (S8).
- S6 — PDF partiellement scanné extrait, pages sans texte journalisées ; `OCR_REQUIRED` seulement sans aucune couche texte.
- S6 — `huggingface-hub` rétrogradé 2.4.0 → 1.33.0 (contrainte `<2` de docling-ibm-models) ; tests E5 verts.
- S6 — polices URW base35 dans l'image : sans police, docling-parse rendait le texte en rectangles dans le conteneur (0 bloc de code). Les tests d'intégration tournent sur l'hôte et ne verraient pas cette régression.
- S6 — CPU par page 7 à 19 × plus élevé en conteneur que sur l'hôte, cause non identifiée.
- S6 — `max_passage_tokens` = 1024 reste Proposed (une seule unité longue mesurée) ; `CHUNKS_PER_UNIT` non mesuré.
- S7 — retrait : `DELETE` 202, tombstone ; retrait répété → 409 sans effet ; pas de `sync_visibility` (exclusion immédiate par le contrôle SQL, I12).
- S7 — purge : points Qdrant d'abord, puis blobs non détenus, puis données SQL + `purged_at` (écart d'ordre vis-à-vis de §6/§10, consigné) ; index vivants seulement (RETIRED supprimé en S9).
- S7 — références de blob : documents non purgés seulement.
- S7 — course résiduelle du nettoyage d'orphelins (import concurrent entre `holders()` et `delete()`, fenêtre ~ms → `BLOB_MISSING`) : documentée, non fermée (S8).
- S7 — renvoyés à S8 : ordonnancement du nettoyage d'orphelins, fichiers `tmp/`, replanification de la purge sur points zombies, `purge_document` FAILED sans hook.
- S8 — O10 : tranché par ADR 0004 point 11 (pas de DLQ ; `FAILED` consultable et rejouable). O14 : la réconciliation adopte l'échec métier quand le hook n'a pas tourné. O15 : clés purgées après 24 h. O16 : `DOCUMENT_TOO_LARGE` au-delà de 1 M tokens d'unités (défaut estimé à partir d'un ratio mesuré de 3,74 caractères/token, pas mesuré sur le PDF de 300 pages).
- S8 — générations de clés `<clé>:<n>` (`enqueue_again`) pour refaire un travail après un item terminé ; `cancel` annule toute la famille (extension du port S0).
- S8 — réouverture d'une purge `FAILED` bornée à 3 (`work_items.reopen_count`, migration `5d2f8c1a7b30`).
- S8 — `--max-tasks-per-child=50` sur `worker_ingestion` ; effet sur la mémoire non mesuré.
- S8 — non traité : coût du parcours complet des points sur grand corpus ; purge des work items terminés (Open S0) ; cycles manqués rejoués en rafale après une longue tâche.
- S9 — ADR 0010 et 0011 (Accepted) : corps inchangé, section de suivi « S9 » ajoutée ; profil définitif proposé par ADR 0013 (Proposed, en attente de validation utilisateur). O1 proposé, O4 tranché (re-parsing depuis le blob), O6 (hybride) reporté avec mesures.
- S9 — chargeur d'embeddings forcé en float32 (Qwen3 déclaré bfloat16 : 3 672 → 297,5 ms/passage sur CPU) ; `WorkQueue.enqueue(delay=)` ; `KB_INDEX_DROP_DELAY_SECONDS=300` (Proposed) ; codes `CHUNK_BATCH_MISSING`, `INDEX_STILL_LIVE` ; règle `MISSING_FROM_BUILDING_INDEX`.
- S9 — non traité : abandon d'un index BUILDING ; déchargement de l'ancien modèle (redémarrage de l'API) ; « un seul BUILDING » garanti par verrou, sans index unique ; coût de la preuve de couverture sur grand corpus ; politique d'extraction toujours celle d'e5-base ; « Qwen3 ne tient pas dans la VM » déduit des mesures hôte, non exécuté dans la VM.

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
- M3 — un seul `Embedder` (E5) par worker : un index d'un autre profil échouerait de façon terminale → fournisseur d'`Embedder` par profil à prévoir en **S9**. **Fait (S9, `EmbedderProvider`).**
- M4 — I11 par booléen `coverage_complete=True` au provisionnement initial : preuve de couverture réelle en **S9** ; Suivi ADR 0011 corrigé. **Fait.**
- M5 — FK différée `active_index_id` → `search_indexes`. **Fait.**
- M6 — surface publique dans 005. **Fait.**
- M7 — composition unique des dépôts UoW / transaction d'indexation. **Fait.**
- M8 — surface sans usage (`PublishDocumentVersionHandler.handle`, champ `visible`) : `handle` retiré, précision §3.6 ; champ `visible` d'`IndexedPoint` conservé jusqu'après S5 (utilisé par les doubles de test). **Partiel.**

Revue tech lead du commit S5 (`2009ad0`) : aucun bloquant, 3 importants, 10 mineurs.
Corrections appliquées (2026-11-18) — 1 136 unitaires, 322 intégration, 3 mutations tuées :

- I1 — `build_knowledge_search` exposait un port interne (`Embedder`) et `register()` créait son propre embedder : un consommateur aurait chargé E5 une seconde fois (~1,1 Gio). Embedder unique dans les ressources du contexte. **Fait.**
- I2 — budget « question comprise » calculé dans l'adapter SQL, non testé → règle dans le domaine, compteur réel. **Fait.**
- I3 — intégrité d'unité non exercée en S5 (unités mono-chunk) → mesure requalifiée, à rendre significative en S6. **Fait.**
- M1 — limites du jeu de référence écrites à côté des chiffres (thèmes disjoints, requêtes filtrées, inter-langues traduites). **Fait.**
- M2, M3, M5, M6, M7 — readiness, borne du budget, bornes `limit` et `text`, question dupliquée après en-tête tronqué. **Fait.**
- M4, M9, M10 — documentés seulement (`CHUNKS_PER_UNIT`, filtres non revérifiés en SQL, import du contrat qui charge le contexte). **Fait.**
- Reliquat S4 M8 — champ `visible` d'`IndexedPoint`. **Fait.**

Incident (2026-11-18, pendant l'écriture des tests rouges S6) : un lancement
de tests d'intégration sans `make test-integration` a visé la base de **dev**
(défauts de `Settings`) ; les fixtures ont vidé `work_items`,
`document_chunks` et `knowledge_units` et créé `public.kb_test_effects`.
Documents, versions, blobs et Qdrant de dev intacts. Garde-fou ajouté dans
`tests/integration/conftest.py` : session refusée si `ENVIRONMENT != test` ou
si `POSTGRES_PORT` vise la stack de dev. Base de dev à reconstruire lors de
la validation S6.

Revue tech lead du commit S6 (`2a53289`) : **1 bloquant**, 3 importants, 8 mineurs.
Corrections appliquées (2026-11-18) — 1 217 unitaires, 364 intégration, 11 mutations tuées, aucun `chunk_hash` existant modifié, test de fumée `make smoke-pdf-image` OK dans l'image :

- B1 — le mapping Docling commun descendait dans les enfants d'un tableau : le texte des cellules mises en forme sortait une seconde fois en paragraphes (Markdown et DOCX ; régression du Markdown S3, reproduite). **Fait.**
- I1 — thread de conversion zombie concurrent de la tâche suivante sur un convertisseur partagé sans verrou (~2 × 2,6 Gio) → verrou par parseur. **Fait.**
- I2 / O16 — décision : garde-fou de taille XML décompressée d'un DOCX (100 Mio, `DOCUMENT_TOO_COMPLEX` terminal) ; borne de volume de texte commune (tokens) reste Open pour S8. **Fait.**
- I3 — rendu des polices non testé dans l'image → test de fumée du parseur PDF dans le conteneur. **Fait.**
- M1–M8 — `PARTIAL_SUCCESS` journalisé, erreurs Docling dans les messages, cohérence liste blanche / routage, factorisation, lignes de tableau, libellés, mesure RSS sur conversions successives. **Fait.**
- Reporté à S8 : le pic RSS d'un enfant croît encore sur 5 conversions PDF successives (1 786 → 2 038 Mio, incréments décroissants ; RSS résiduelle stable ~1,4 Gio) → décider `max-tasks-per-child` ou mesure plus longue.

Revue tech lead du commit S7 (`317c421`) : aucun bloquant, 3 importants, 4 mineurs.
Corrections appliquées (2026-11-18) — 1 321 unitaires, 395 intégration, 8 mutations tuées :

- I1 — course `CleanBlobOrphans` / import du même contenu : un fichier fraîchement réécrit pouvait être supprimé → `BLOB_MISSING` terminal et contenu définitivement non réimportable (`UNIQUE(content_hash)` global). Suppression conditionnelle (quarantaine, inode + mtime, relecture des références). **Fait.**
- I2 — ordre de purge non conforme à l'ADR 0012 Accepted (point 6 : points → chunks → blobs) et Suivi contradictoire. Consigne appliquée : l'ADR prévaut → ordre rétabli (points, SQL, puis blobs au mieux). **Fait.**
- I3 — clé `purge_document:<id>` non replanifiable (enqueue renvoie l'item terminé) : contrainte documentée pour S8. **Fait.**
- M1–M4 — docstring de défense en profondeur, guide (409 au rejeu d'un `DELETE`), test unitaire discriminant du rejeu du retrait. **Fait.**
- Écart accepté : quarantaine dans `KB_BLOB_ROOT/quarantine/` (et non `tmp/`, que le nettoyage S8 des temporaires viderait). Reprise des fichiers de quarantaine après un crash → S8.

Revue tech lead du commit S8 (`8944d04`) : aucun bloquant, 3 importants, 9 mineurs.
Corrections appliquées (2026-11-18) — 1 584 unitaires, 490 intégration (sur stack neuve ; un premier passage avait échoué sur `test_qdrant` à cause de collections laissées par des bases isolées d agents), 11 mutations tuées :

- I1 — codes dépendant de la configuration ou du déploiement (`DOCUMENT_TOO_LARGE`, `TOO_MANY_PAGES`, `NOT_FINALIZED`, `UNKNOWN_KIND`) non rejouables : avec O2, contenu bloqué pour toujours → rejouables à la main. **Fait.**
- I2 — un item de réconciliation par créneau (288/jour), cycles `FAILED` dans `/failures`, rafale après une longue tâche → pas de nouvel item si un cycle attend ; cycles exclus de `/failures`. **Fait.**
- I3 — réparation d'une dérive de manifest sur une version publiée : tous les points réécrits invisibles → document absent de la recherche jusqu'à la synchronisation. Décision : ne réécrire que les points en écart (règle « écrits invisibles » de l'ADR 0010 conservée). **Fait.**
- M1–M9 — test de concurrence adoption O14 / rejeu, transaction par document, `reopen_count` relu sous verrou, statut en enum, codes validés contre le catalogue, renommage, port déplacé, décompte `COMPLETED_NOT_PUBLISHED`, `max-tasks-per-child`. **Fait.**

Revue tech lead du commit S9 (`50b12ba`) : aucun bloquant, 4 importants, 10 mineurs.

- I1 — migration bloquée sans issue : commande `abandon <index_id>` (`AbandonIndexMigration`, travail annulé sous fencing, collection supprimée après délai) ; message de `MigrationInProgressError` corrigé. **Fait.**
- I2 — « un seul BUILDING » porté par `KnowledgeCorpus.provision_index` + index unique partiel (migration `40038a21e22f`) ; deux `start` concurrents testés sur PostgreSQL réel. **Fait.**
- I3 — double mode `embedder=`/`embedders=` supprimé. **Fait.**
- I4 — `sync_visibility` FAILED bloque l'activation ; `enqueue_again(reuse_running=False)` pour la resynchronisation. **Fait.**
- M1, M2, M4–M8, M10 — **Faits** (M10 non mesuré). M9 → point O17 (§16). M3 sans action (documenté ADR 0013).
- Signalés hors périmètre : `cancel_indexing` (S7) n'annule pas les `rechunk_version` (sans effet, `prepare` rend `None`) ; items FAILED d'un index abandonné listés dans `/failures` (rejeu refusé `OBSOLETE_WORK` d'après le code, non testé spécifiquement).

Validation globale (commit `91c5b8c`, [rapport](knowledge-base-validation.md)) : 9 points OK, 5 défauts mineurs.

- D1 — fabrique `test_parser` collectée comme test : `__test__ = False`. **Fait** (revalidation finale : 1 735 unitaires/architecture, 545 intégration — le test fantôme en moins —, 31 avertissements).
- D2 — refus du domaine en trace brute par `runtime.index_migration` → base publique `IndexOperationRefusedError`, message `refusé : …` sur stderr, code 1. **Fait.**
- D3 — healthcheck du scheduler sur l'âge du fichier de planification beat (WAL `dbm.sqlite3`, réécrit toutes les 180 s ; seuil 300 s). **Fait** (healthy sur la stack de dev).
- D4 — aucune route REST d'état d'un document/version (suivi par psql). À décider par l'utilisateur.
- D5 — `TerminalWorkError` journalisée en une ligne WARNING avec son code, sans trace ; erreurs inattendues et hook en échec en ERROR avec trace. **Fait.** (`NOT_FINALIZED`/`UNKNOWN_KIND` suivent aussi ce chemin.)
- M-1 (S2) — refus anticipé 413 `PAYLOAD_TOO_LARGE` avant lecture du corps quand `Content-Length` > `KB_MAX_UPLOAD_BYTES` + 64 Kio (enveloppe multipart) ; contrôle en flux conservé. **Fait.** Revalidation : 1 759 unitaires/architecture, 548 intégration.
