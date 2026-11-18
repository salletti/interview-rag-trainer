.PHONY: help install lint format format-check typecheck test test-integration \
        test-integration-up test-integration-down ci up down build logs ps \
        migrate migration worker-ping frontend-install frontend-lint frontend-test

BACKEND := cd backend && uv run
FRONTEND := cd frontend && npm run

# Environnement de la stack compose.test.yaml (hôte).
INTEGRATION_ENV := ENVIRONMENT=test POSTGRES_HOST=localhost POSTGRES_PORT=5441 \
	POSTGRES_USER=irt_test POSTGRES_PASSWORD=irt_test POSTGRES_DB=irt_test \
	QDRANT_HOST=localhost QDRANT_PORT=6343 \
	RABBITMQ_HOST=localhost RABBITMQ_PORT=5682 RABBITMQ_USER=irt_test RABBITMQ_PASSWORD=irt_test \
	KB_TEST_CONSUMER_TIMEOUT_SECONDS=5 \
	SENTENCE_TRANSFORMERS_HOME=$(CURDIR)/backend/.cache/models \
	HF_HOME=$(CURDIR)/backend/.cache/huggingface HF_HUB_DISABLE_XET=1
# KB_TEST_CONSUMER_TIMEOUT_SECONDS active le scénario 14 de S0 : il doit
# valoir le consumer_timeout de backend/tests/integration/rabbitmq_test/
# 20-consumer-timeout.conf (en secondes).
# SENTENCE_TRANSFORMERS_HOME : cache local (ignoré par git) des poids du
# modèle des tests `model` (S4), téléchargés une fois au premier lancement.
# HF_HOME : idem pour les poids de Docling PDF (S6 : layout, TableFormer).

help: ## Liste des cibles
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-24s %s\n", $$1, $$2}'

# --- Backend -----------------------------------------------------------------
install: ## Installe les dépendances backend (uv) et frontend (npm)
	cd backend && uv sync
	cd frontend && npm ci

lint: ## Ruff check + format check
	$(BACKEND) ruff check .
	$(BACKEND) ruff format --check .

format: ## Ruff format + corrections automatiques
	$(BACKEND) ruff check --fix .
	$(BACKEND) ruff format .

typecheck: ## mypy strict
	$(BACKEND) mypy

test: ## Tests unitaires et d'architecture (sans service externe)
	$(BACKEND) pytest

test-integration-up:
	docker compose -f compose.test.yaml up -d --wait --build

test-integration-down:
	docker compose -f compose.test.yaml down -v

test-integration: test-integration-up ## Tests d'intégration contre compose.test.yaml (dont les tests `model`, vrai E5)
	cd backend && export $(INTEGRATION_ENV); \
	uv run alembic upgrade head; status=$$?; \
	if [ $$status -eq 0 ]; then uv run pytest -m integration; status=$$?; fi; \
	cd .. && $(MAKE) test-integration-down; \
	exit $$status

ci: lint typecheck test frontend-lint frontend-test ## Équivalent local de la CI

# --- Frontend ----------------------------------------------------------------
frontend-lint: ## ESLint, Prettier (check) et tsc
	$(FRONTEND) lint
	$(FRONTEND) format:check
	$(FRONTEND) typecheck

frontend-test: ## Vitest
	$(FRONTEND) test

# --- Docker Compose ----------------------------------------------------------
up: ## Démarre toute la stack de développement
	docker compose up --build -d

down: ## Arrête la stack (conserve les volumes)
	docker compose down

build: ## Reconstruit les images
	docker compose build

logs: ## Suit les logs
	docker compose logs -f --tail=100

ps: ## État des services
	docker compose ps

migrate: ## Applique les migrations dans la stack de dev
	docker compose run --rm migrate

migration: ## Crée une migration vide : make migration m="slug"
	docker compose run --rm migrate alembic revision -m "$(m)"

worker-ping: ## Vérifie l'aller-retour broker → worker → result backend
	docker compose exec api python -c "from runtime.worker import ping; print(ping.delay().get(timeout=10))"
