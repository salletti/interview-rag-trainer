# ADR 0005 — Routage multi-LLM et budgets

- **Date :** 2026-11-16
- **Statut :** Proposed

## Proposition

Dissimuler les providers derrière un port EvaluationEngine et un adaptateur LiteLLM. Fournisseurs envisagés : Groq, Cerebras, Gemini, OpenRouter. Politique free-only par défaut, aucune bascule implicite vers le payant.

## Motivation

Réduire les interruptions provoquées par les quotas, sans contaminer le domaine Assessment avec les détails du fournisseur.

## Risques

Différences de JSON structuré, tool calling et qualité des modèles ; quotas évolutifs ; confidentialité. Valider les outputs et conserver provenance, tests mocks et vrais providers.
