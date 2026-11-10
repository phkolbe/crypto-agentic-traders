#!/usr/bin/env bash
# Cria a pasta de producao do Crypto (padrao /opt/crypto) com .env, crypto.env e
# backup.env, senhas novas e modo 600, e a pasta backups/ em 700. Nunca sobrescreve: se o
# arquivo existe, para.
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
senha() { head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32; }

for f in .env crypto.env backup.env; do
  [ -e "$PASTA/$f" ] && { echo "$PASTA/$f ja existe: nada foi alterado." >&2; exit 1; }
done

# O umask vem ANTES do mkdir: a pasta dos dumps nascia 0755 (criada antes do umask) e os
# dumps, com o historico de negociacoes, ficavam legiveis por qualquer usuario da maquina.
umask 077
mkdir -p "$PASTA/backups"
chmod 700 "$PASTA/backups"
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
RESTIC_PASSWORD=$(senha)
VIGIA_URL=
EOF
cp "$RAIZ/crypto.env.exemplo" "$PASTA/crypto.env"
cat >"$PASTA/backup.env" <<EOF
# Credenciais do provedor do repositorio restic (lidas pelo backup.sh). Modo 600.
# Ex.: AWS_ACCESS_KEY_ID= / AWS_SECRET_ACCESS_KEY=   ou   B2_ACCOUNT_ID= / B2_ACCOUNT_KEY=
EOF
chmod 600 "$PASTA/.env" "$PASTA/crypto.env" "$PASTA/backup.env"
echo "Pasta $PASTA pronta:"
echo "  .env        VERSAO_CRYPTO, TS_AUTHKEY (primeira subida) e, se houver, OTEL_ENDPOINT e VIGIA_URL"
echo "  crypto.env  nasce em dry_run; live e chaves da Binance depois (ver o arquivo)"
echo
echo "OBRIGATORIO antes do live: copia do banco FORA deste servidor. Os dumps de"
echo "$PASTA/backups ficam no mesmo disco; perder a VM perde o historico de negociacoes."
echo "  1. criar um bucket/repositorio num provedor (S3, B2...) so do Crypto;"
echo "  2. RESTIC_REPOSITORY no .env (a RESTIC_PASSWORD ja foi gerada: copie-a para o"
echo "     gerenciador de senhas -- sem ela, o repositorio nao abre depois de perder a VM);"
echo "  3. credenciais do provedor em backup.env;"
echo "  4. rodar backup.sh uma vez e restaurar.sh --ensaio (README, Backup e restauracao)."
