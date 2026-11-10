#!/usr/bin/env bash
# Implanta (ou atualiza) o Sistema Cripto em producao. Cofre Obsidian cryptocoins:
# implantacao.md e acessos-e-operacao.md.
#
#     sudo implantacao/producao/implantar.sh                 # servidor (pasta /opt/crypto)
#     sudo implantacao/producao/implantar.sh --sem-backup    # primeira subida
#     PASTA=C:/teste implantacao/producao/implantar.sh --local   # teste na maquina de dev
#
# Ordem: imagens -> Postgres (espera saudavel) -> backup (se ja ha schema) -> migrar ->
# sistema (espera saudavel, ate IMPLANTAR_ESPERA_SEGUNDOS, padrao 180) -> confere a trava
# (sem o login, 403) -> no Tailscale -> registra a versao em $PASTA/versao-no-ar.
# `--local`: sem pull e sem o no Tailscale (a API fica so na rede da stack; a trava e
# conferida igual).
#
# VOLTA AUTOMATICA. Se o `migrar` falha, o sistema novo nao fica saudavel no prazo ou a
# trava nao recusa quem nao tem o login, o script volta para a versao ANTERIOR (a ultima
# registrada em versao-no-ar; sem o registro, a imagem do crypto-prd que rodava) e sai
# com 1. Antes, saia com 1 deixando a versao nova quebrada no ar. O banco decide como:
#   - schema igual (o `migrar` da nova nao aplicou migracao): so a imagem volta, o banco
#     fica como esta -- a anterior o aceita;
#   - schema mudou, fora do live (dry_run, testnet): a anterior recusa banco migrado por
#     versao mais nova (db/migracoes.py). Restaura o dump feito antes do `migrar`
#     (restaurar.sh --confirmar; o estado da nova fica em backups/antes-de-restaurar-*) e
#     volta. Perde-se so o que a nova simulou nesses minutos;
#   - schema mudou, em LIVE: NAO restaura sozinho. O dump e de antes da nova subir; o que
#     ela gravou desde entao (ordens e trades reais) sumiria do banco e ficaria so na
#     Binance, e a restauracao deixa o sistema parado ate alguem conferir. A nova fica
#     como esta (o vigia a reinicia), o script diz os comandos da volta e sai com 1.
# O .env continua pedindo a versao nova: a proxima implantacao tenta de novo. Para ficar
# na anterior, trocar VERSAO_CRYPTO no .env.
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
ESPERA="${IMPLANTAR_ESPERA_SEGUNDOS:-180}"
for f in .env crypto.env; do
  [ -f "$PASTA/$f" ] || { echo "Falta $PASTA/$f (preparar.sh)." >&2; exit 1; }
done
valor() { grep "^$1=" "$2" | tail -1 | cut -d= -f2- || true; }
passo() { printf '\n== %s\n' "$*"; }
alarme() { echo "$*" >&2; logger -t crypto-implantacao -p user.err "$*" 2>/dev/null || true; }

VERSAO="$(valor VERSAO_CRYPTO "$PASTA/.env")"
[ -n "$VERSAO" ] || { echo "Defina VERSAO_CRYPTO em $PASTA/.env." >&2; exit 1; }
MODO="$(valor TRADING_MODE "$PASTA/crypto.env")"
MODO="${MODO:-dry_run}"
C=(docker compose -p crypto-prd --env-file "$PASTA/.env" -f "$RAIZ/compose.yml")

# A versao para onde voltar: a ultima que passou por uma implantacao inteira. Sem o
# registro (primeira implantacao com este script), a imagem do crypto-prd que esta rodando.
ANTERIOR="$(cat "$PASTA/versao-no-ar" 2>/dev/null || true)"
if [ -z "$ANTERIOR" ]; then
  imagem="$(docker inspect -f '{{.Config.Image}}' crypto-prd 2>/dev/null || true)"
  case "$imagem" in ghcr.io/phkolbe/crypto:*) ANTERIOR="${imagem#ghcr.io/phkolbe/crypto:}" ;; esac
fi

passo "Sistema Cripto $VERSAO, modo $MODO${ANTERIOR:+ (no ar: $ANTERIOR)}"
if [ "$MODO" = live ] && [ -z "$(valor RESTIC_REPOSITORY "$PASTA/.env")" ]; then
  echo "AVISO: live SEM copia do banco fora do servidor (RESTIC_REPOSITORY vazio; README, Backup e restauracao)." >&2
fi
if [ "$MODO" = live ] && [ -z "$(valor VIGIA_URL "$PASTA/.env")" ]; then
  echo "AVISO: live SEM dead-man's switch (VIGIA_URL vazio): processo travado ou host fora do ar nao avisam ninguem (README, Vigia)." >&2
fi

if [ "$LOCAL" = 0 ] && [ "$VERSAO" != local ]; then
  passo "Imagens"
  "${C[@]}" pull -q
fi

passo "Postgres"
"${C[@]}" up -d --wait postgres

versao_schema() {
  # 0 sem a tabela (banco vazio ou anterior as migracoes versionadas).
  docker exec crypto-prd-postgres psql -U postgres -d crypto -tAc \
    "SELECT coalesce(max(versao), 0) FROM schema_versao" 2>/dev/null || echo 0
}

tabelas="$(docker exec crypto-prd-postgres psql -U postgres -d crypto -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")"
SCHEMA_ANTES=0
DUMP=""
if [ "$tabelas" -gt 0 ]; then
  SCHEMA_ANTES="$(versao_schema)"
fi
if [ "$SEM_BACKUP" = 0 ] && [ "$tabelas" -gt 0 ]; then
  passo "Backup antes de migrar"
  PASTA="$PASTA" "$RAIZ/backup.sh"
  # shellcheck disable=SC2012
  DUMP="$(ls -1t "$PASTA"/backups/crypto-*.dump 2>/dev/null | head -1 || true)"
fi

esperar_saude() {
  local estado=ausente
  for _ in $(seq 1 $(( (ESPERA + 2) / 3 ))); do
    estado="$(docker inspect -f '{{.State.Health.Status}}' crypto-prd 2>/dev/null || echo ausente)"
    [ "$estado" = healthy ] && break
    sleep 3
  done
  echo "  crypto-prd: $estado"
  [ "$estado" = healthy ]
}

# De dentro do conteiner, fora do serve: so a sonda de saude passa. Sem o cabecalho e
# COM o login do dono forjado (o que o host ou outro conteiner da rede conseguem
# mandar): os dois tem de dar 403.
trava() {
  docker exec -e LOGIN_FORJADO="${1:-}" crypto-prd python -c "
import os, urllib.error, urllib.request
pedido = urllib.request.Request('http://127.0.0.1:8000/crypto-traders/api/portfolio')
if os.environ['LOGIN_FORJADO']:
    pedido.add_header('Tailscale-User-Login', os.environ['LOGIN_FORJADO'])
try:
    urllib.request.urlopen(pedido, timeout=5)
    print(200)
except urllib.error.HTTPError as erro:
    print(erro.code)
"
}
conferir_trava() {
  local codigo
  codigo="$(trava || true)"
  [ "$codigo" = 403 ] || { echo "A API NAO recusa quem nao tem o login ($codigo)." >&2; return 1; }
  echo "  sem o login: 403"
  codigo="$(trava "$(valor CRYPTO_LOGIN "$PASTA/.env")" || true)"
  [ "$codigo" = 403 ] || { echo "A API aceita o login do Tailscale vindo de fora do serve ($codigo)." >&2; return 1; }
  echo "  login forjado fora do serve: 403"
}

voltar_atras() {
  if [ -z "$ANTERIOR" ] || [ "$ANTERIOR" = "$VERSAO" ]; then
    alarme "A $VERSAO nao subiu e nao ha para onde voltar (sem versao anterior registrada, ou e a mesma)."
    return 1
  fi
  local schema_depois=0
  [ "$tabelas" -gt 0 ] && schema_depois="$(versao_schema)"
  if [ "$schema_depois" != "$SCHEMA_ANTES" ]; then
    # O migrar da nova aplicou migracao: a anterior recusa este banco.
    if [ -z "$DUMP" ]; then
      alarme "A $VERSAO nao subiu e migrou o schema ($SCHEMA_ANTES -> $schema_depois), mas esta implantacao nao fez backup: sem volta automatica. A $ANTERIOR recusa este banco."
      return 1
    fi
    if [ "$MODO" = live ]; then
      alarme "LIVE: a $VERSAO nao subiu e migrou o schema ($SCHEMA_ANTES -> $schema_depois); a $ANTERIOR recusa este banco. NAO restaurado sozinho: o dump de antes apagaria as ordens e trades que a $VERSAO gravou desde entao. Para voltar:
  1. sudo implantacao/producao/restaurar.sh --confirmar $DUMP
  2. conferir na Binance as ordens e trades posteriores ao dump e lancar o que faltar;
  3. VERSAO_CRYPTO=$ANTERIOR em $PASTA/.env e implantacao/producao/implantar.sh"
      return 1
    fi
    passo "Restaurando o banco de antes do migrar ($MODO; schema $schema_depois -> $SCHEMA_ANTES)"
    PASTA="$PASTA" "$RAIZ/restaurar.sh" --confirmar "$DUMP" || {
      alarme "A restauracao de $DUMP falhou: a $ANTERIOR nao sobe neste banco. Precisa de alguem."
      return 1
    }
  fi
  passo "Voltando para $ANTERIOR"
  VERSAO_CRYPTO="$ANTERIOR" "${C[@]}" up -d --no-deps crypto || true
  if esperar_saude && conferir_trava; then
    alarme "A $VERSAO falhou; VOLTOU para $ANTERIOR (saudavel, trava conferida). O $PASTA/.env ainda pede VERSAO_CRYPTO=$VERSAO."
    return 0
  fi
  alarme "A $VERSAO falhou e nem a $ANTERIOR ficou saudavel com a trava fechada. Precisa de alguem."
  return 1
}

falhou() {
  docker logs --tail 40 crypto-prd-migrar >&2 || true
  docker logs --tail 60 crypto-prd >&2 || true
  echo "$1" >&2
  voltar_atras || true
  exit 1
}

passo "Migrar e subir o sistema"
"${C[@]}" up -d crypto || falhou "O migrar ou a subida da $VERSAO falhou."
esperar_saude || falhou "O sistema nao subiu. Em live sem as chaves da Binance ele recusa subir de proposito."

passo "Trava de acesso"
conferir_trava || falhou "A trava de acesso da $VERSAO esta aberta."

if [ "$LOCAL" = 0 ]; then
  passo "No Tailscale"
  "${C[@]}" up -d tailscale
  if [ -n "$(valor TS_AUTHKEY "$PASTA/.env")" ] && [ "$SEM_BACKUP" = 0 ]; then
    echo "AVISO: TS_AUTHKEY ainda no $PASTA/.env (e no ambiente do conteiner tailscale): apagar e implantar de novo." >&2
  fi
fi

printf '%s\n' "$VERSAO" >"$PASTA/versao-no-ar"
logger -t crypto-implantacao "Sistema Cripto $VERSAO" 2>/dev/null || true
echo
echo "No ar: https://$(valor TS_HOSTNAME "$PASTA/.env").$(valor TAILNET "$PASTA/.env")/crypto-traders/ (so $(valor CRYPTO_LOGIN "$PASTA/.env"))"
