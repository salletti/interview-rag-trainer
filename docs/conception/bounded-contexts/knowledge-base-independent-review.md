# Knowledge Base — dossier de review indépendante (Codex)

> **Statut : Pending Independent Review.** La review indépendante demandée
> pour clôturer l'objectif S0–S9 **n'a pas été effectuée** : Codex n'est pas
> exécutable dans l'environnement de développement (aucun binaire `codex` dans
> le `PATH`, aucune application installée ; constats du 2026-11-17 et du
> 2026-11-19). Ce dossier contient tout ce qu'il faut pour la lancer.
> Les revues par slice faites par un agent tech lead (S2 à S9, consignées dans
> [le suivi](knowledge-base-progress.md)) **ne remplacent pas** cette review.

## 1. Objet

Relire, **sur les sources** et non sur un résumé, l'implémentation complète du
bounded context Knowledge Base livrée sur la branche
`feature/knowledge-base-v1` (19 commits depuis `main`, S0 → corrections S9),
et rendre un rapport de défauts localisés.

## 2. Comment lancer la review

Depuis la racine du dépôt, sur la branche à relire :

```bash
git fetch origin && git switch feature/knowledge-base-v1
git diff --stat main...feature/knowledge-base-v1   # périmètre
codex   # puis coller le prompt de la section 6
```

Pour reproduire les vérifications (stack de test isolée, jamais la stack de
dev) :

```bash
make lint typecheck test      # Ruff, mypy strict, unitaires + architecture
make test-integration         # PostgreSQL, Qdrant, RabbitMQ, E5 réels (compose.test.yaml)
```

## 3. Périmètre

| Zone | Chemins |
|---|---|
| Domaine | `backend/src/knowledge_base/domain/` (entités, value objects, services, ports `providers/`) |
| Cas d'usage | `backend/src/knowledge_base/use_cases/` |
| Adapters | `backend/src/knowledge_base/adapters/` (REST, tâches Celery, persistance SQLAlchemy, Qdrant, embeddings, parsing Docling, blobs) |
| Configuration et surface publique | `backend/src/knowledge_base/frameworks/`, `backend/src/knowledge_base/__init__.py`, `backend/src/knowledge_base/contracts/` |
| Runtime | `backend/src/runtime/` (application, workers, scheduler, `index_migration`, `provision`) |
| Work queue partagée | `knowledge_base.work_items` et son adapter `sqlalchemy_work_queue.py` |
| Migrations | `backend/alembic/versions/` (chaîne KB : `7c1a0e2b9d41` → … → `5d2f8c1a7b30` → `40038a21e22f`) |
| Exploitation | `compose.yaml`, `compose.test.yaml`, `Makefile`, `backend/Dockerfile`, `.env.example` |
| Tests | `backend/tests/{unit,integration,architecture}/` |
| Référentiel | ADR 0004, 0007 à 0013 ; `docs/conception/bounded-contexts/knowledge-base*.md` |

## 4. Points sensibles à examiner en priorité

1. **Work queue (ADR 0004)** : lease, heartbeat, fencing token, `recover`,
   `max_attempts` au claim, générations `<clé>:<n>`, `cancel` par famille,
   `reopen_count`. Rechercher toute écriture finale non gardée par le fencing
   et tout chemin qui promettrait un exactly-once sur un effet externe.
2. **Index dérivé (ADR 0010)** : points écrits `visible=false` puis
   `sync_visibility` ; recherche filtrée `visible=true` puis contrôle SQL
   (PUBLISHED/ACTIVE) ; manifest et digest ; réconciliation (Qdrant lu avant
   PostgreSQL).
3. **Blue/green (ADR 0011, 0013)** : barrière `FOR UPDATE` / `FOR SHARE`,
   couverture I11 calculée à l'activation, garde de synchronisation
   (PENDING/RUNNING/FAILED), un seul index `BUILDING` (agrégat + index unique
   partiel), `abandon`, suppression différée de la collection
   (`KB_INDEX_DROP_DELAY_SECONDS`), rechunking.
4. **Purge et stockage (ADR 0012)** : ordre points → SQL avec fencing et
   `purged_at` → blobs après commit ; nettoyage des orphelins avec quarantaine
   (contrôle inode + mtime, relecture des détenteurs) ; course avec un import
   concurrent du même contenu.
5. **Extraction (ADR 0008)** : contrôles pypdfium2 avant layout
   (`INVALID_DOCUMENT`, `TOO_MANY_PAGES`, `OCR_REQUIRED`), garde-fou XML DOCX,
   verrou par parseur, conversion annulée sur heartbeat refusé, cellules de
   tableau.
6. **Catalogue d'erreurs** : axes `automatic_retry` / `replayable`, cohérence
   avec `RetryIngestion` et `POST /failures/{id}/retry`.
7. **Frontières** : `runtime` ne voit que la racine `knowledge_base` ;
   `contracts/` en stdlib seule ; domaine sans framework ; aucun autre BC
   n'accède aux tables, à Qdrant ou aux fichiers de KB.
8. **Sécurité des entrées** : limites de taille, `Content-Length`, noms de
   fichiers, chemins de blobs, requête de recherche (≤ 2000 caractères,
   `limit` ≤ 50).

## 5. Écarts et limites déjà connus (à ne pas redécouvrir comme nouveaux)

Consignés dans [le suivi](knowledge-base-progress.md) (« Écarts et
décisions ») et au §16 de [la conception](knowledge-base.md) :
- refus anticipé sur `Content-Length` non traité (M-1 de S2) ;
- pic RSS du worker d'ingestion (`--max-tasks-per-child=50`, effet non mesuré) ;
- purge des work items terminés non implémentée (Open S0) ;
- coût du parcours complet des points Qdrant sur un grand corpus non mesuré ;
- O16 (`DOCUMENT_TOO_LARGE` > 1 M tokens) estimé, non mesuré ;
- O17 : collection Qdrant orpheline possible après un refus concurrent de
  migration ;
- politique d'extraction figée sur e5-base (ADR 0013) ;
- ADR 0013 et la barrière de l'ADR 0011 au statut **Proposed**.

Un défaut qui aggrave l'un de ces points reste un constat valable.

## 6. Prompt à donner à Codex

```text
Tu fais la review indépendante de la branche feature/knowledge-base-v1 du
dépôt courant (bounded context Knowledge Base, slices S0 à S9), par rapport
à main. Travaille sur les SOURCES (git diff main...HEAD et les fichiers
complets), pas sur les résumés ni sur les rapports de suivi.

Référentiel à lire d'abord : CLAUDE.md ; docs/decisions/0004, 0007 à 0013 ;
docs/conception/bounded-contexts/knowledge-base.md ;
docs/conception/bounded-contexts/knowledge-base-independent-review.md
(sections 3 à 5 : périmètre, points sensibles, limites déjà connues).
Les ADR « Accepted » font foi ; signale tout écart du code à leur égard.

Cherche en priorité : violations de fencing ou de lease, effets externes non
idempotents ou présentés comme exactly-once, courses entre transactions
(verrous, ordre des effets Qdrant/SQL/fichiers), pertes ou fuites de
visibilité dans la recherche, migrations Alembic incorrectes ou non
réversibles, violations de frontières entre contextes, entrées non bornées,
tests qui passeraient même si le code était faux.

Tu peux exécuter `make lint typecheck test` et `make test-integration`
(stack compose.test.yaml). N'exécute rien contre la stack de dev
(ports 5440, 8010) et ne modifie aucun fichier.

Rends un rapport en français, un constat par entrée :
- fichier:ligne
- sévérité : bloquant | important | mineur
- description du défaut
- impact concret
- reproduction (scénario ou test à écrire) et correction suggérée
Termine par la liste de ce que tu n'as pas pu vérifier, puis un tableau
Problème / Avant / Après dont la colonne Avant ne contient que des faits
établis.
```

## 7. Après la review

1. Consigner le rapport (fichier `knowledge-base-independent-review-report.md`).
2. Corriger les constats **bloquants**, avec test rouge d'abord, puis relancer
   `make lint typecheck test` et `make test-integration`.
3. Mettre à jour [le suivi](knowledge-base-progress.md) : statut global
   « Terminé » seulement quand la review est faite et les bloquants corrigés.
