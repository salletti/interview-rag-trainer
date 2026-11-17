# CLAUDE.md — Interview RAG Trainer

Règles permanentes pour toute intervention (humaine ou agent). Ce fichier
référence la documentation ; il ne la remplace pas.

## Documentation First

Avant toute analyse, décision ou modification :

1. Lire [README.md](README.md) et [docs/README.md](docs/README.md).
2. Lire les documents pertinents de `docs/context/`, `docs/conception/`
   (y compris `docs/conception/bounded-contexts/`), `docs/decisions/` et
   `docs/guides/`, en entier pour les ADR et pour le contexte concerné.
3. Identifier les décisions **Accepted**, **Proposed** et les points **Open**
   ([index des ADR](docs/decisions/README.md)).
4. Vérifier les conventions de structure et de dépendances :
   [005 — Structure](docs/conception/005-project-structure.md) et
   `backend/tests/architecture/`.

Une proposition n'est jamais une décision acceptée.

## Décisions

- Ne jamais modifier silencieusement un ADR **Accepted** : proposer un nouvel
  ADR qui le remplace explicitement (statut Superseded).
- Une décision structurante nouvelle donne lieu à un ADR, validé avant
  l'implémentation.
- Les paramètres à mesurer (délais, seuils, modèle d'embeddings) restent
  Proposed jusqu'à leur mesure.

## Architecture

- Monolithe modulaire, DDD, Clean Architecture ([ADR 0003](docs/decisions/0003-modular-monolith-clean-architecture.md)).
- Bounded Contexts : Interview Training, Assessment ([ADR 0001](docs/decisions/0001-two-bounded-contexts.md)),
  Knowledge Base ([ADR 0007](docs/decisions/0007-knowledge-base-bounded-context.md)).
- Frontières : un contexte n'importe d'un autre que ses `contracts/` ; aucun
  accès aux tables, à Qdrant ou aux fichiers d'un autre contexte ; `runtime`
  ne voit que la surface publique des contextes ; le domaine ne dépend
  d'aucun framework.
- Traitements durables : table `work_items`, lease, fencing, at-least-once à
  effets idempotents, jamais de promesse exactly-once
  ([ADR 0004](docs/decisions/0004-reliable-messaging.md)).

## Développement

- Par **vertical slices**, dans l'ordre de la feuille de route du contexte
  (Knowledge Base : [S0–S9](docs/conception/bounded-contexts/knowledge-base-roadmap.md)).
  Ne créer que les dossiers et abstractions qu'une slice utilise.
- **Tests obligatoires** : unitaires (domaine sans I/O), intégration contre
  les vrais services (`make test-integration`), architecture. Signaler tout
  test non exécuté.
- Exécution par l'environnement du projet (`uv`, `npm`, Docker Compose,
  `Makefile`) ; aucune installation globale sur l'hôte.
- Ports hôte et commandes : [guide environnement local](docs/guides/environnement-local.md).

## Documentation synchronisée

- Toute modification de code met à jour la documentation concernée (conception,
  README de contexte, guide, ADR) dans le même changement.
- Documents de travail rédigés en français.
- Travail sur une branche dédiée, Pull Request vers `main`, pas de merge
  automatique sauf demande explicite.
