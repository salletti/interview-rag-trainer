# ADR 0002 — Chorégraphie par événements en V1

- **Date :** 2026-11-16
- **Statut :** Accepted

## Contexte

Le cycle Submission → Assessment → Feedback implique deux BC sans besoin immédiat de coordinateur.

## Décision

- Interview Training émet AnswerSubmitted.v1.
- Assessment consomme, évalue et émet AssessmentPublished.v1 ou AssessmentFailed.v1 si échec terminal.
- Interview Training projette le résultat, Assessment ne fait pas avancer l'entretien.
- Commandes et invariants locaux restent synchrones ; communication de faits entre BC asynchrone.

## Alternative

Un Process Manager explicite est repoussé tant que le workflow reste linéaire.

## Conséquences

Contrats d'événements versionnés, idempotence, cohérence éventuelle et récupération des pannes. La technologie du broker n'est pas figée par cet ADR.
