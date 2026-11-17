# ADR 0004 — Messagerie fiable et idempotence

- **Date :** 2026-11-16 (complété le 2026-11-17, acquittement arbitré le 2026-11-18)
- **Statut :** **Accepted pour le complément « Work queue durable »**
  (2026-11-17, acquittement 2026-11-18), y compris la séparation entre work
  queue et Integration Events et le principe at-least-once à effets
  idempotents ; **Proposed** pour l'implémentation de la messagerie
  inter-contextes (outbox, inbox, topologie des exchanges).

## Proposition

Transactional Outbox PostgreSQL par producteur ; inbox atomique par consommateur ; livraison at-least-once ; contraintes uniques métier ; retries bornés ; leases/fencing pour appels LLM longs ; recovery et reconciliation.

## Justification

Le commit PostgreSQL et la publication RabbitMQ ne forment pas une même transaction atomique. Les redeliveries existent. On n'ouvre pas de transaction pendant les appels réseau LLM.

## Vigilances

ACK après commit ; event_id stable en republication ; messages d'échec seulement terminaux ; jobs de récupération ; possible coût de double appel fournisseur après crash.

## À trancher

Topologie RabbitMQ et Celery ; durable work queue, dead-letter, TTL leases et politique de reconciliation.

> Mise à jour 2026-11-17 : la *durable work queue*, la topologie des files de
> Knowledge Base, les TTL de lease, la politique de reconciliation de
> l'ingestion et l'absence de dead-letter en V1 sont tranchés par le complément
> ci-dessous. Restent à trancher : topologie des exchanges pour les Integration
> Events, outbox et inbox (premier slice inter-contextes).

---

## Complément — Work queue durable (Accepted, 2026-11-17)

### Contexte

Knowledge Base exécute des traitements longs et coûteux (extraction,
embeddings, écritures Qdrant) déclenchés par des transactions métier. Un
appel `celery.delay()` après commit peut être perdu (crash entre commit et
publication, message perdu, worker tué) ; avant commit, il peut exécuter un
travail dont la transaction échoue. Ni l'un ni l'autre n'est durable.

### Deux mécanismes distincts

| | Transactional Outbox | Work queue durable |
|---|---|---|
| Objet | **fait** publié vers d'autres contextes (Integration Event) | **travail** interne à exécuter par le contexte lui-même |
| Consommateurs | autres Bounded Contexts, via RabbitMQ | handlers du même contexte, via Celery |
| Table | outbox du producteur (à venir) | `<contexte>.work_items`, d'abord `knowledge_base.work_items` |
| Garantie | publication at-least-once, `event_id` stable, inbox côté consommateur | exécution at-least-once, finalisation au plus une fois par fencing |
| Statut | Proposed | **Accepted** |

Un même handler peut écrire dans les deux dans la même transaction (par
exemple publier `knowledge.document_version_published.v1` lors d'une
publication) ; ils ne sont jamais fusionnés.

### Décision

1. **Table `work_items` par contexte**, écrite dans la transaction métier qui
   crée le besoin (même Unit of Work). Elle porte statut, leases, fencing
   tokens, tentatives, prochaine disponibilité (backoff) et dernière erreur.
   Ces mécanismes **ne sont pas dupliqués** dans les agrégats métier.
2. **Le message RabbitMQ est un signal de déclenchement** qui ne transporte
   que l'identifiant de l'item ; **PostgreSQL reste la source de vérité** du
   travail à accomplir. Plusieurs notifications peuvent concerner le même item.
   Un `delay()` n'est jamais considéré comme une garantie de traitement.

   **Acquittement anticipé (`acks_late = False`)** pour les tâches pilotées
   par `work_items` : Celery acquitte le message juste avant l'exécution. Cet
   acquittement **n'est pas une garantie de traitement** : la récupération ne
   dépend jamais d'une redélivrance RabbitMQ, mais du redispatch des items
   `PENDING`, de la reprise des leases expirés et des invariants
   transactionnels (claim conditionnel, fencing). `acks_late = True` est écarté
   car il ajoute un second chemin de reprise, qui peut réexécuter un item
   pendant que sa tentative d'origine tourne encore, sans garantie
   supplémentaire. Le délai d'acquittement du broker (`consumer_timeout`,
   configurable et dépendant du type de file) n'est qu'un cas de ce risque.
   Détail et scénarios de panne :
   [S0 §8.1 et §8.6](../conception/bounded-contexts/knowledge-base-s0.md#81-messages-et-acquittement-accepted).
3. **Dispatch après commit** par une tâche périodique (scheduler Celery beat),
   avec redispatch des items réveillés mais jamais pris en charge, et plafond
   d'items en vol par file.
4. **Claim conditionnel** avec lease et fencing token incrémenté ; une
   redélivrance trouve l'item déjà pris ou terminé et ne fait rien.
5. **Finalisation sous fencing** : les écritures métier et le passage
   `SUCCEEDED` sont dans la même transaction, conditionnée au token ; une
   tentative périmée est annulée par rollback.
6. **Idempotence métier** : le fencing garantit au plus une finalisation
   PostgreSQL ; il **ne garantit pas** l'exécution unique des effets externes.
   Ceux-ci (Qdrant, fichiers) doivent être idempotents (identité déterministe,
   écrasement sans accumulation, suppression par filtre) et récupérables.
   Aucune garantie « exactly once » n'est donnée pour un effet externe.
7. **Retries** : erreurs transitoires et inattendues → nouvelle tentative avec
   backoff exponentiel plafonné et jitter ; nombre de tentatives borné par
   type de travail ; erreurs déterministes → échec terminal immédiat.
8. **Reprise** : un lease expiré (crash, OOM, limite de temps) remet l'item en
   attente, ou en échec terminal si les tentatives sont épuisées.
9. **Réconciliation** propre au contexte, planifiée, pour les écarts que le
   cycle normal ne corrige pas (Knowledge Base : ADR 0010).
10. **Échecs terminaux** visibles en base (`FAILED`, code et message),
    relançables par une commande explicite.
11. **Pas de DLQ RabbitMQ en V1** pour la work queue : le message ne
    transporte qu'un identifiant et l'état `FAILED` en base joue le rôle de
    dead-letter, consultable et relançable. À réexaminer lorsque des
    consommateurs d'Integration Events (inbox) seront introduits.
12. **Topologie Knowledge Base** : file `knowledge_base.ingestion` consommée par
    un worker dédié (`--concurrency=1`) ; tâches de dispatch et de reprise sur
    la file par défaut ; une seule instance de beat.

Le schéma, les transitions, les paramètres et les tests sont décrits dans
[knowledge-base-s0.md](../conception/bounded-contexts/knowledge-base-s0.md).

### Alternatives envisagées

- **`delay()` direct** : non durable.
- **Worker PostgreSQL sans Celery** (`SKIP LOCKED` en boucle) : viable, mais
  Celery et RabbitMQ sont déjà installés et retenus (ADR 0006).
- **Acquittement tardif Celery (`acks_late = True`)** : la redélivrance
  deviendrait un second mécanisme de reprise, concurrent du scheduler, qui
  peut relancer un item encore en cours (fermeture de canal, redémarrage du
  broker, délai d'acquittement dépassé) ; il ne rend visible aucun échec côté
  métier.
- **DLQ RabbitMQ** : un second endroit à surveiller, invisible du domaine.

### Conséquences

- Nouveaux processus de la même image : worker d'ingestion et scheduler.
- Les handlers distinguent effets externes (hors transaction, idempotents) et
  finalisation (transactionnelle, sous fencing).
- Le mécanisme vit d'abord dans Knowledge Base ; il ne sera extrait vers
  `shared` que lorsqu'un deuxième contexte (Assessment) en aura besoin.

### Risques et compromis

- Latence de prise en charge bornée par l'intervalle de dispatch, et non
  immédiate.
- Une tentative zombie peut répéter un effet externe : accepté sous les
  conditions d'idempotence ci-dessus.
- Scheduler en instance unique : doublons inoffensifs en cas d'erreur de
  déploiement, mais à surveiller.

### Conditions de réévaluation

- Introduction des Integration Events (outbox, inbox, DLQ).
- Besoin d'une latence de prise en charge plus faible (notification
  post-commit).
- Partage du mécanisme avec un deuxième contexte (extraction vers `shared`).

### Liens

- ADR [0002](0002-event-choreography-v1.md), [0006](0006-technical-bootstrap.md),
  [0007](0007-knowledge-base-bounded-context.md),
  [0010](0010-postgresql-source-of-truth-qdrant-derived-index.md).
- [002 — Flux](../conception/002-event-flows.md),
  [knowledge-base.md §6–§7](../conception/bounded-contexts/knowledge-base.md#6-workflow-de-traitement-avec-work_items).
