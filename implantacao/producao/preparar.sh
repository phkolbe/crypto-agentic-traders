#!/usr/bin/env bash
# Cria a pasta de producao do Crypto (padrao /opt/crypto) com .env e crypto.env, senhas
# novas e modo 600. Nunca sobrescreve: se o arquivo existe, para.
#
#     sudo implantacao/producao/preparar.sh --tailnet <rede>.ts.net --login <e-mail>
#     PASTA=C:/algum/lugar implantacao/producao/preparar.sh ...    # teste na maquina de dev
set -euo pipefail

TAILNET="" LOGIN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --tailnet) TAILNET="${2:?}"; shift ;;
    --login) LOGIN="${2:?}"; shift ;;
    *) echo "opcao desconhecida: $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$TAILNET" ] && [ -n "$LOGIN" ] || { echo "uso: preparar.sh --tailnet <rede>.ts.net --login <e-mail>" >&2; exit 2; }

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASTA="${PASTA:-/opt/crypto}"
mkdir -p "$PASTA/backups"
senha() { head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32; }

for f in .env crypto.env; do
  [ -e "$PASTA/$f" ] && { echo "$PASTA/$f ja existe: nada foi alterado." >&2; exit 1; }
done

umask 077
cat >"$PASTA/.env" <<EOF
# Gerado por preparar.sh em $(date -u +%Y-%m-%dT%H:%MZ). Modo 600. Explicacao: env.exemplo.
PASTA=$PASTA
VERSAO_CRYPTO=
TS_HOSTNAME=crypto
TAILNET=$TAILNET
TS_AUTHKEY=
CRYPTO_LOGIN=$LOGIN
OTEL_ENDPOINT=
SENHA_DB_POSTGRES=$(senha)
SENHA_DB_CRYPTO_DONO=$(senha)
SENHA_DB_CRYPTO=$(senha)
RESTIC_REPOSITORY=
RESTIC_PASSWORD=
EOF
cp "$RAIZ/crypto.env.exemplo" "$PASTA/crypto.env"
chmod 600 "$PASTA/.env" "$PASTA/crypto.env"
echo "Pasta $PASTA pronta:"
echo "  .env        VERSAO_CRYPTO, TS_AUTHKEY (primeira subida) e, se houver, OTEL_ENDPOINT e restic"
echo "  crypto.env  nasce em dry_run; live e chaves da Binance depois (ver o arquivo)"
