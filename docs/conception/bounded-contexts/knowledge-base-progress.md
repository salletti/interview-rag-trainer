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
| S3 | Docling Markdown, `KnowledgeUnit`, chunking | S2 | À faire | | |
| S4 | Embeddings, Qdrant, index initial, publication | S3 | À faire | | |
| S5 | Recherche sémantique, provenance, évaluation | S4 | À faire | | |
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

## Corrections en attente

Revue tech lead du commit S2 (`2fc83da`) : aucun bloquant. À appliquer juste
après S3 (fichiers partagés avec l'implémentation S3 en cours) :

- I-1 — verrou optimiste : la première lecture d'un document dans une UoW doit faire foi (`find_by_content_hash` ne doit pas écraser `lock_version`) ; test d'intégration avec commit concurrent entre les deux lectures.
- I-2 — même `Idempotency-Key` concurrente sur `/versions` : inclure `ConcurrentModificationError` dans le rejeu unique.
- I-3 — `knowledge-base.md` §4.1 I3 : aligner sur `UNIQUE(content_hash)` global (renvoi O2).
- M-2 fabriques des handlers injectables ; M-3 basename + longueur bornée du nom de fichier ; M-4 I1 en base (trigger ou texte ajusté) ; M-5 `ImportDocumentResult` commun ; M-6 expiration des clés d'idempotence → point ouvert §16 ; M-7 test HTTP `DOCUMENT_WITHDRAWN`.
- M-1 — libellé « streaming » de la feuille de route corrigé (fait) ; refus précoce sur `Content-Length` à évaluer.
