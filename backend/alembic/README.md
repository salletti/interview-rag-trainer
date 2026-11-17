# Migrations Alembic

- `uv run alembic upgrade head` applique les migrations (asyncpg, URL lue dans `runtime.settings`).
- `uv run alembic revision -m "<slug>"` crée une migration vide ; `--autogenerate` fonctionnera dès qu'un Bounded Context aura enregistré son `MetaData` dans `env.py`.
- Jamais de `metadata.create_all()` pour faire évoluer un schéma : Alembic est l'unique mécanisme.
- Dans Docker Compose, le service `migrate` exécute `upgrade head` avant l'API.
