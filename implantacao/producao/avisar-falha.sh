#!/usr/bin/env bash
# Aviso de que uma unidade do systemd do Sistema Cripto falhou (hoje o backup):
# `OnFailure=crypto-falha@%n.service` no crypto-backup.service roda
#
#     implantacao/producao/avisar-falha.sh <unidade>      # ex.: crypto-backup.service
#
# Por que nao o e-mail do proprio sistema: o backup falha justamente quando o Postgres ou o
# disco estao com problema, e o sistema pode estar caido junto. O aviso sai por fora:
#   1. journal com prioridade de erro (`journalctl -p err -t crypto-falha`);
#   2. com VIGIA_URL no .env, ping em "<url>/fail" com o fim do log da unidade no corpo
#      (convencao do healthchecks.io: o check vai para "down" e o servico avisa o dono).
#      O proximo ping de vida do vigia (1 min) devolve o check a "up" -- o aviso e o "down".
#
# Nunca falha (sai 0): um aviso que quebra nao pode virar outra unidade com falha.
set -uo pipefail

UNIDADE="${1:-desconhecida}"
PASTA="${PASTA:-/opt/crypto}"
valor() { [ -r "$2" ] && grep "^$1=" "$2" | tail -1 | cut -d= -f2- ; }
URL="$(valor VIGIA_URL "$PASTA/.env" || true)"

fim_do_log="$(journalctl -u "$UNIDADE" -n 15 --no-pager -o cat 2>/dev/null | tail -c 2000)"
echo "crypto-falha: $UNIDADE falhou" >&2
logger -t crypto-falha -p user.err "FALHOU: $UNIDADE" 2>/dev/null || true

if [ -n "$URL" ]; then
  printf 'Sistema Cripto: %s falhou em %s\n\n%s\n' "$UNIDADE" "$(hostname 2>/dev/null)" "$fim_do_log" |
    curl -fsS -m 10 --retry 2 -o /dev/null --data-binary @- "$URL/fail" ||
    echo "crypto-falha: ping de falha nao saiu" >&2
fi
exit 0
