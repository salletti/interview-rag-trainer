# ADR 0004 — Messagerie fiable et idempotence

- **Date :** 2026-11-16
- **Statut :** Proposed

## Proposition

Transactional Outbox PostgreSQL par producteur ; inbox atomique par consommateur ; livraison at-least-once ; contraintes uniques métier ; retries bornés ; leases/fencing pour appels LLM longs ; recovery et reconciliation.

## Justification

Le commit PostgreSQL et la publication RabbitMQ ne forment pas une même transaction atomique. Les redeliveries existent. On n'ouvre pas de transaction pendant les appels réseau LLM.

## Vigilances

ACK après commit ; event_id stable en republication ; messages d'échec seulement terminaux ; jobs de récupération ; possible coût de double appel fournisseur après crash.

## À trancher

Topologie RabbitMQ et Celery ; durable work queue, dead-letter, TTL leases et politique de reconciliation.
