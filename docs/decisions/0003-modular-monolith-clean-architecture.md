# ADR 0003 — Monolithe modulaire, Clean Architecture

- **Date :** 2026-11-16 (accepté le 2026-11-18)
- **Statut :** Accepted

## Décision

Une application déployable, des BC comme packages src/<bounded_context> : domain, use_cases, adapters, contracts (si utile), frameworks (si utile). runtime est le composition root. Dépendances vers les abstractions internes.

## Origine

Conventions de salletti/trading-agent-ai, en particulier docs/conception/006-project-structure.md.

## Différence motivée

Integration Events JSON versionnés séparés des Domain Events internes : publication durable et moindre couplage entre contextes.

## Conséquences

Tests domaine indépendants, tests de contrats inter-contextes et composition explicite des adapters.

## Vérification de cohérence (2026-11-17)

Le contenu reste conforme aux orientations retenues pour Knowledge Base
(ADR [0007](0007-knowledge-base-bounded-context.md)) : un seul déployable (une
image backend, plusieurs processus : API, workers Celery, scheduler), des BC
sous `src/<bounded_context>`, `runtime` comme composition root. La surface
publique d'un contexte est précisée dans
[005](../conception/005-project-structure.md#surface-publique-dun-contexte-et-composition-root) :
plusieurs points d'entrée nommés (`register`, `register_worker`, fabriques de
contrats) au lieu de `register(app)` seul. Cette précision ne modifie pas la
décision.

**Acceptation (2026-11-18)** : décision validée explicitement lors de la
clôture de la conception Knowledge Base, avec les conventions de
[005](../conception/005-project-structure.md) (Accepted à la même date).
