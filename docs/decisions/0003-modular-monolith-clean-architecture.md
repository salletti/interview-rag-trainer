# ADR 0003 — Monolithe modulaire, Clean Architecture

- **Date :** 2026-11-16
- **Statut :** Proposed

## Proposition

Une application déployable, des BC comme packages src/<bounded_context> : domain, use_cases, adapters, contracts (si utile), frameworks (si utile). runtime est le composition root. Dépendances vers les abstractions internes.

## Origine

Conventions de salletti/trading-agent-ai, en particulier docs/conception/006-project-structure.md.

## Différence motivée

Integration Events JSON versionnés séparés des Domain Events internes : publication durable et moindre couplage entre contextes.

## Conséquences

Tests domaine indépendants, tests de contrats inter-contextes et composition explicite des adapters.
