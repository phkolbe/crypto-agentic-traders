#!/usr/bin/env bash
# `crypto-traders check` em producao, num conteiner PROPRIO e descartavel.
#
#     sudo implantacao/producao/diagnostico.sh
#
# Nunca `docker exec crypto-prd crypto-traders check`: o diagnostico carrega o
# catalogo da exchange (~200 MB) e, dentro do conteiner do sistema, divide os 512 MB
# dele -- o kernel mata o conteiner inteiro, e com ele quem executa o stop (medido em
# 04/11/2026, no desenvolvimento). O proprio `check` recusa rodar ali.
#
# Mesma imagem, mesmo crypto.env, mesma rede (banco e exchange): so o processo e outro.
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASTA="${PASTA:-/opt/crypto}"
[ -f "$PASTA/.env" ] || { echo "Falta $PASTA/.env (preparar.sh)." >&2; exit 1; }

exec docker compose -p crypto-prd --env-file "$PASTA/.env" -f "$RAIZ/compose.yml" \
  run --rm --no-deps crypto crypto-traders check
