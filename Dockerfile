# syntax=docker/dockerfile:1.10
# Imagem do Sistema Cripto (desenvolvimento em Docker e producao). Mesmo molde das
# imagens da plataforma (cofre Obsidian grupo-sr, plataforma/arquitetura/
# implantacao-ambientes.md §9): tres estagios -- telas (npm), dependencias (uv) e
# execucao (python slim, usuario sem privilegio). O Crypto nao usa o pampufa-sdk,
# entao nao ha token no build.
#
#     docker build -t ghcr.io/phkolbe/crypto:local .
#     docker buildx build --platform linux/arm64 -t ghcr.io/phkolbe/crypto:vX.Y.Z .
#
# A mesma imagem roda o `migrar` (schema e gatilhos, com o usuario dono do banco) e
# o `run` (agentes + API + telas, com o usuario da aplicacao). Segredos e modo de
# operacao vem so do ambiente do conteiner; nada de .env dentro da imagem.

ARG PYTHON_IMAGE=python:3.13-slim-bookworm

# --- telas: frontend/dist ---------------------------------------------------------
FROM node:22-bookworm-slim AS telas
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- dependencias: /app/.venv -----------------------------------------------------
FROM ${PYTHON_IMAGE} AS deps
COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY backend/src backend/src
# Modo editavel: PROJECT_ROOT (config.py) continua sendo /app, onde fica frontend/dist.
# Extra `postgres` (asyncpg); o barramento e o in-process (um processo so).
RUN uv sync --locked --no-dev --extra postgres --no-cache

# --- execucao ---------------------------------------------------------------------
FROM ${PYTHON_IMAGE}
RUN useradd --uid 10001 --user-group --no-create-home --home-dir /app servico
WORKDIR /app
COPY --from=deps /app /app
COPY --from=telas /src/frontend/dist frontend/dist
# data/: so o caminho padrao do SQLite (nao usado com DATABASE_URL); fica gravavel.
RUN mkdir -p data && chown servico:servico data
# API_HOST=0.0.0.0 so DENTRO do conteiner: a porta nunca e publicada fora do
# 127.0.0.1 (dev) nem fora da rede do ambiente (producao, atras da borda).
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 API_HOST=0.0.0.0 API_PORT=8000 \
    DB_MIGRAR_NA_SUBIDA=false LOG_JSON=true TRADING_MODE=dry_run
USER servico
EXPOSE 8000
# /api/health responde so com o orquestrador de pe (503 sem ele).
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"API_PORT\"]}/api/health', timeout=4)"]
# exec form: o processo e o PID 1 e recebe o SIGTERM do `docker stop` (desligamento limpo).
STOPSIGNAL SIGTERM
CMD ["crypto-traders", "run"]
