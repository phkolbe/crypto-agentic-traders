#!/usr/bin/env bash
# Restauracao do banco do Sistema Cripto a partir de um dump do backup.sh -- e o ENSAIO dela.
#
#     sudo implantacao/producao/restaurar.sh --ensaio [<arquivo.dump>]     # mensal; nao toca no `crypto`
#     sudo implantacao/producao/restaurar.sh --confirmar <arquivo.dump>    # restauracao de verdade
#
# Ensaio: restaura o dump (sem arquivo, o mais recente de /opt/crypto/backups) num database
# descartavel `crypto_ensaio` do mesmo Postgres, confere que as tabelas e os gatilhos
# append-only sao os do `crypto`, mostra as contagens e apaga o descartavel (mesmo se
# falhar). Backup que nunca foi restaurado nao e backup: o ensaio e o que prova que o
# dump volta. Rodar todo mes e depois de mudar o schema.
#
# De verdade:
#   1. copia o estado atual em backups/antes-de-restaurar-<data>.dump (fora da rotacao);
#   2. para o crypto-prd (`docker stop`; o vigia nao religa conteiner parado);
#   3. pg_restore --clean --if-exists --single-transaction: um erro no meio desfaz TUDO e
#      o banco fica como estava. Sem a transacao unica, o --clean apagava as tabelas e o
#      pg_restore seguia depois do erro, deixando o banco meio restaurado;
#   4. NAO religa o sistema. Em live, o banco voltou para a data do dump e as ordens e
#      trades posteriores existem so na Binance: conferir (Binance x tela Trades/Portfolio)
#      e lancar o que faltar antes de religar -- o preco medio do stop vem desse historico.
#      Religar: implantacao/producao/implantar.sh.
#
# O dump traz tabelas, gatilhos, GRANTs e ALTER DEFAULT PRIVILEGES; os usuarios crypto e
# crypto_dono e o GRANT CONNECT vem do postgres_init.sh (servidor novo: implantar.sh
# --sem-backup primeiro, que cria o volume, e depois restaurar).
set -euo pipefail

PASTA="${PASTA:-/opt/crypto}"
PG="${CONTEINER_PG:-crypto-prd-postgres}"
ENSAIO_DB=crypto_ensaio

MODO=""
case "${1:-}" in
  --ensaio) MODO=ensaio; shift ;;
  --confirmar) MODO=real; shift ;;
esac
arquivo="${1:-}"
if [ -z "$MODO" ] || { [ "$MODO" = real ] && [ -z "$arquivo" ]; }; then
  echo "uso: restaurar.sh --ensaio [<arquivo.dump>]  |  restaurar.sh --confirmar <arquivo.dump>" >&2
  echo "     --confirmar SOBRESCREVE o banco de producao (ver o cabecalho do script)." >&2
  exit 2
fi
if [ -z "$arquivo" ]; then
  # shellcheck disable=SC2012
  arquivo="$(ls -1t "$PASTA"/backups/crypto-*.dump 2>/dev/null | head -1 || true)"
fi
[ -n "$arquivo" ] && [ -s "$arquivo" ] || { echo "dump nao encontrado ou vazio: ${arquivo:-<nenhum>}" >&2; exit 1; }

consulta() {  # $1 = database, $2 = SQL
  docker exec "$PG" psql -U postgres -v ON_ERROR_STOP=1 -tA -d "$1" -c "$2"
}
restaurar_em() {  # $1 = database; o resto vai para o pg_restore
  local alvo="$1"
  shift
  docker exec -i "$PG" pg_restore -U postgres -d "$alvo" --single-transaction --exit-on-error "$@" <"$arquivo"
}
TABELAS="SELECT coalesce(string_agg(table_name, ',' ORDER BY table_name), '') FROM information_schema.tables WHERE table_schema = 'public'"
GATILHOS="SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE NOT t.tgisinternal AND n.nspname = 'public'"

if [ "$MODO" = ensaio ]; then
  apagar_ensaio() { docker exec "$PG" dropdb -U postgres --if-exists "$ENSAIO_DB" >/dev/null 2>&1 || true; }
  trap apagar_ensaio EXIT
  echo "== Ensaio de restauracao: $arquivo -> $ENSAIO_DB (o banco crypto nao e tocado)"
  apagar_ensaio
  docker exec "$PG" createdb -U postgres -O crypto_dono "$ENSAIO_DB"
  restaurar_em "$ENSAIO_DB"
  tabelas_dump="$(consulta "$ENSAIO_DB" "$TABELAS")"
  tabelas_prd="$(consulta crypto "$TABELAS")"
  gatilhos_dump="$(consulta "$ENSAIO_DB" "$GATILHOS")"
  gatilhos_prd="$(consulta crypto "$GATILHOS")"
  if [ -z "$tabelas_dump" ] || [ "$tabelas_dump" != "$tabelas_prd" ]; then
    echo "ENSAIO FALHOU: as tabelas do dump nao sao as do banco crypto" >&2
    echo "  dump:   $tabelas_dump" >&2
    echo "  crypto: $tabelas_prd" >&2
    exit 1
  fi
  if [ "$gatilhos_dump" -eq 0 ] || [ "$gatilhos_dump" != "$gatilhos_prd" ]; then
    echo "ENSAIO FALHOU: gatilhos no dump $gatilhos_dump, no banco crypto $gatilhos_prd" >&2
    exit 1
  fi
  echo "  tabelas e gatilhos ($gatilhos_dump) iguais aos do banco crypto; linhas no dump:"
  IFS=, read -r -a lista <<<"$tabelas_dump"
  for tabela in "${lista[@]}"; do
    printf '    %-28s %s\n' "$tabela" "$(consulta "$ENSAIO_DB" "SELECT count(*) FROM \"$tabela\"")"
  done
  echo "ENSAIO OK"
  logger -t crypto-restauracao "ensaio ok $arquivo" 2>/dev/null || true
  exit 0
fi

echo "== Restauracao de $arquivo SOBRE o banco crypto de producao"
umask 077
antes="$PASTA/backups/antes-de-restaurar-$(date -u +%Y%m%d-%H%M%S).dump"
if docker exec "$PG" pg_dump -U postgres -d crypto -Fc >"$antes" && [ -s "$antes" ]; then
  echo "  estado atual guardado em $antes"
else
  rm -f "$antes"
  echo "  AVISO: nao deu para guardar o estado atual (banco ja danificado?); seguindo" >&2
fi
echo "  parando crypto-prd"
docker stop -t 60 crypto-prd >/dev/null 2>&1 || true
if [ "$(docker inspect -f '{{.State.Running}}' crypto-prd 2>/dev/null || echo false)" = true ]; then
  echo "crypto-prd continua rodando: restauracao cancelada (o banco nao foi tocado)" >&2
  exit 1
fi
restaurar_em crypto --clean --if-exists
logger -t crypto-restauracao "restaurado $arquivo" 2>/dev/null || true
echo "Restaurado. O sistema continua PARADO."
echo "Em live: conferir na Binance as ordens e trades posteriores ao dump e lancar o que faltar"
echo "antes de religar (o preco medio do stop vem do historico). Religar: implantar.sh."
