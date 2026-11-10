#!/usr/bin/env bash
# Implanta (ou atualiza) o Sistema Cripto em producao. Cofre Obsidian cryptocoins:
# implantacao.md e acessos-e-operacao.md.
#
#     sudo implantacao/producao/implantar.sh                 # servidor (pasta /opt/crypto)
#     sudo implantacao/producao/implantar.sh --sem-backup    # primeira subida
#     PASTA=C:/teste implantacao/producao/implantar.sh --local   # teste na maquina de dev
#
# Ordem: imagens -> Postgres (espera saudavel) -> backup (se ja ha schema) -> migrar ->
# sistema (espera saudavel, ate 3 min) -> confere a trava (sem o login, 403) -> no
# Tailscale. Para na primeira falha. `--local`: sem pull e sem o no Tailscale (a API
# fica so na rede da stack; a trava e conferida igual).
set -euo pipefail

LOCAL=0
SEM_BACKUP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --local) LOCAL=1 ;;
    --sem-backup) SEM_BACKUP=1 ;;
    *) echo "opcao desconhecida: $1" >&2; exit 2 ;;
  esac
  shift
done

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASTA="${PASTA:-/opt/crypto}"
for f in .env crypto.env; do
  [ -f "$PASTA/$f" ] || { echo "Falta $PASTA/$f (preparar.sh)." >&2; exit 1; }
done
valor() { grep "^$1=" "$2" | tail -1 | cut -d= -f2-; }
passo() { printf '\n== %s\n' "$*"; }

VERSAO="$(valor VERSAO_CRYPTO "$PASTA/.env")"
[ -n "$VERSAO" ] || { echo "Defina VERSAO_CRYPTO em $PASTA/.env." >&2; exit 1; }
C=(docker compose -p crypto-prd --env-file "$PASTA/.env" -f "$RAIZ/compose.yml")

passo "Sistema Cripto $VERSAO, modo $(valor TRADING_MODE "$PASTA/crypto.env")"

if [ "$LOCAL" = 0 ] && [ "$VERSAO" != local ]; then
  passo "Imagens"
  "${C[@]}" pull -q
fi

passo "Postgres"
"${C[@]}" up -d --wait postgres

tabelas="$(docker exec crypto-prd-postgres psql -U postgres -d crypto -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")"
if [ "$SEM_BACKUP" = 0 ] && [ "$tabelas" -gt 0 ]; then
  passo "Backup antes de migrar"
  PASTA="$PASTA" "$RAIZ/backup.sh"
fi

passo "Migrar e subir o sistema"
"${C[@]}" up -d crypto
for _ in $(seq 1 60); do
  estado="$(docker inspect -f '{{.State.Health.Status}}' crypto-prd 2>/dev/null || echo ausente)"
  [ "$estado" = healthy ] && break
  sleep 3
done
echo "  crypto-prd: $estado"
if [ "$estado" != healthy ]; then
  docker logs --tail 40 crypto-prd-migrar >&2 || true
  docker logs --tail 60 crypto-prd >&2 || true
  echo "O sistema nao subiu. Em live sem as chaves da Binance ele recusa subir de proposito." >&2
  exit 1
fi

passo "Trava de acesso"
# De dentro do conteiner, sem o cabecalho do Tailscale: so a sonda de saude passa.
codigo="$(docker exec crypto-prd python -c "
import urllib.error, urllib.request
try:
    urllib.request.urlopen('http://127.0.0.1:8000/crypto-traders/api/portfolio', timeout=5)
    print(200)
except urllib.error.HTTPError as erro:
    print(erro.code)
")"
[ "$codigo" = 403 ] || { echo "A API NAO recusa quem nao tem o login ($codigo)." >&2; exit 1; }
echo "  sem o login: 403"

if [ "$LOCAL" = 0 ]; then
  passo "No Tailscale"
  "${C[@]}" up -d tailscale
fi

logger -t crypto-implantacao "Sistema Cripto $VERSAO" 2>/dev/null || true
echo
echo "No ar: https://$(valor TS_HOSTNAME "$PASTA/.env").$(valor TAILNET "$PASTA/.env")/crypto-traders/ (so $(valor CRYPTO_LOGIN "$PASTA/.env"))"
