# syntax=docker/dockerfile:1.10
# Imagem do Sistema Cripto (desenvolvimento em Docker e producao, implantacao/producao/).
# Tres estagios -- telas (npm), dependencias (uv) e execucao (python slim, usuario sem
# privilegio). Nenhum segredo no build.
#
#     docker build -t ghcr.io/phkolbe/crypto:local .
#     docker buildx build --platform linux/arm64 -t ghcr.io/phkolbe/crypto:vX.Y.Z .
#
# A mesma imagem roda o `migrar` (schema e gatilhos, com o usuario dono do banco) e
# o `run` (agentes + API + telas, com o usuario da aplicacao). Segredos e modo de
# operacao vem so do ambiente do conteiner; nada de .env dentro da imagem.
#
# Imagens base fixadas por DIGEST (a versao exata no comentario): a tag `3.13-slim` ou
# `22-bookworm-slim` muda de conteudo a cada correcao do upstream, e o mesmo commit
# gerava imagens diferentes conforme o dia do build. O digest e o do indice
# multiplataforma (serve ao arm64 da producao e ao amd64 da CI). Para atualizar (de
# proposito, num commit proprio, com a CI construindo a imagem):
#     docker buildx imagetools inspect python:3.13-slim-bookworm   # linha "Digest:"
# e trocar digest e versao aqui; o mesmo para node e uv (README da producao, "Imagens").

# python 3.13.18 (slim-bookworm)
ARG PYTHON_IMAGE=python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72

# --- telas: frontend/dist ---------------------------------------------------------
# node 22.23.4 (bookworm-slim)
FROM node:22-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d AS telas
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- dependencias: /app/.venv -----------------------------------------------------
FROM ${PYTHON_IMAGE} AS deps
# uv 0.11.24
COPY --from=ghcr.io/astral-sh/uv:0.11.24@sha256:99ea34acedc870ba4ad11a1f540a1c04267c9f30aadc465a94406f52dfda2c36 /uv /bin/uv
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
# A sonda pergunta se o sistema PROTEGE a carteira (`?sonda=1`, Orchestrator.vitalidade):
# 503 com agente critico sem batida, morto ou desistido, ou com o laco do watchdog
# parado. So "o objeto existe" deixava o conteiner healthy com o stop parado. O Docker
# nao reinicia conteiner unhealthy: quem reinicia e o vigia do host
# (implantacao/producao/systemd/crypto-vigia.*).
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"API_PORT\"]}/crypto-traders/api/health?sonda=1', timeout=4)"]
# exec form: o processo e o PID 1 e recebe o SIGTERM do `docker stop` (desligamento limpo).
STOPSIGNAL SIGTERM
CMD ["crypto-traders", "run"]
