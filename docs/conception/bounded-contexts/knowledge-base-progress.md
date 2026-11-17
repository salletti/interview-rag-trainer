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
| S0 | Infrastructure de traitement durable | — | À faire | | |
| S1 | `KnowledgeDocument` et `DocumentVersion` | S0 | À faire | | |
| S2 | Import Markdown/TXT, stockage, dédoublonnage | S0, S1 | À faire | | |
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

## Écarts et décisions en cours de route

_(aucun pour l'instant)_
