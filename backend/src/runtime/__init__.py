"""Composition root technique du monolithe. Aucune règle métier.

Assemble l'application FastAPI et le worker Celery à partir de la
configuration (``runtime.settings``) et des ressources techniques (PostgreSQL,
Qdrant, RabbitMQ). Les Bounded Contexts y seront enregistrés par leur surface
publique uniquement, jamais par leurs couches internes.
"""
