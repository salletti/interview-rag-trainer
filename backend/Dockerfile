# syntax=docker/dockerfile:1

FROM python:3.13-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN groupadd --system app && useradd --system --gid app --no-create-home --home-dir /app app
# Polices pour le rendu des pages PDF par docling-parse (S6) : sans elles, les
# polices standard non embarquées (Helvetica, Courier…) sont rendues en
# rectangles et le modèle de layout ne reconnaît plus titres ni code (mesuré :
# 0 bloc de code sur les fixtures). URW base35 : clones des 35 polices
# PostScript standard (sorties identiques à l'hôte macOS sur les fixtures ;
# Liberation, essayée, classait encore un bloc de code en paragraphe) ;
# DejaVu : repli de docling-parse.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends fonts-urw-base35 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app

# --- development : dépendances de dev, code monté en volume, rechargement ---
FROM base AS development
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable \
    && chown -R app:app /app
# Points de montage des volumes de Knowledge Base (kb_blobs, models_cache),
# possédés par app : un volume nommé neuf copie les droits du répertoire.
RUN mkdir -p /var/lib/interview-rag-trainer/blobs /var/cache/interview-rag-trainer/models \
    && chown -R app:app /var/lib/interview-rag-trainer /var/cache/interview-rag-trainer
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src"
USER app
EXPOSE 8000
CMD ["uvicorn", "runtime.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# --- builder : dépendances de production uniquement ---------------------------
FROM base AS builder
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# --- production : image minimale, utilisateur non-root -----------------------
FROM python:3.13-slim AS production
RUN groupadd --system app && useradd --system --gid app --no-create-home --home-dir /app app
# Mêmes polices que l'étape base (rendu des pages PDF, S6).
RUN apt-get update \
    && apt-get install --yes --no-install-recommends fonts-urw-base35 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder --chown=app:app /app /app
RUN mkdir -p /var/lib/interview-rag-trainer/blobs /var/cache/interview-rag-trainer/models \
    && chown -R app:app /var/lib/interview-rag-trainer /var/cache/interview-rag-trainer
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"]
CMD ["uvicorn", "runtime.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
