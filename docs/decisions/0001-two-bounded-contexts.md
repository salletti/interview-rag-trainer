# ADR 0001 — Séparer Interview Training et Assessment

- **Date :** 2026-11-16
- **Statut :** Accepted

## Contexte

Déroulement d'entretien et évaluation de réponse possèdent vocabulaires, règles et invariants indépendants.

## Décision

Deux Bounded Contexts : **Interview Training** (sessions, tours, soumissions) et **Assessment** (barèmes, corrections, notes, révisions). Aucun accès aux entités ou ORM du voisin.

## Alternatives

Un unique BC aurait été plus simple, mais l'évaluation doit pouvoir être réutilisée hors d'un entretien.

## Conséquences

Contrats inter-contextes, projection de résultats côté entretien, eventual consistency et évolution autonome du modèle Assessment.
