"""Application Celery de production et tâche technique de vérification.

RabbitMQ est le broker ; le result backend ``rpc://`` réutilise RabbitMQ, ce
qui évite d'installer Redis pour un simple aller-retour technique.

Les tâches Celery ne sont **pas** les Integration Events entre Bounded
Contexts (docs/conception/004-integration-contracts.md) : ceux-ci seront des
contrats JSON versionnés publiés via une outbox dans une phase ultérieure.

La composition (tâches de Knowledge Base, ressources des processus enfants)
est faite par ``runtime.worker_factory.create_celery_app``.

Lancement : ``celery -A runtime.worker:celery_app worker`` (pool prefork) ;
``celery -A runtime.worker:celery_app beat`` pour le scheduler.
"""

from __future__ import annotations

from runtime.worker_factory import create_celery_app

celery_app = create_celery_app()
# Un enfant prefork démarré en spawn (défaut de billiard sous macOS) ne
# reconstruit les tâches qu'en important les modules de ``include`` : ce module
# de composition doit en faire partie (sous Linux, les enfants sont forkés).
celery_app.conf.include = [__name__]


@celery_app.task(name="runtime.ping")
def ping() -> str:
    """Tâche technique : prouve que broker, worker et result backend
    fonctionnent de bout en bout."""
    return "pong"
