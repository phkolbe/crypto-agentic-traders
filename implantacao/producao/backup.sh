#!/usr/bin/env bash
# Backup do banco do Sistema Cripto em producao: `pg_dump` (formato custom) em
# /opt/crypto/backups, guardando os 14 mais recentes, e, com RESTIC_REPOSITORY no .env,
# uma copia cifrada no repositorio restic do Crypto (7 diarios, 4 semanais, 6 mensais).
# Credenciais do provedor do restic (AWS_*, B2_*...) em /opt/crypto/backup.env.
#
#     sudo implantacao/producao/backup.sh       # tambem pelo timer do systemd, 03:30
#
# Restaurar: com o sistema parado (docker stop crypto-prd),
#     docker exec -i crypto-prd-postgres pg_restore -U postgres -d crypto --clean --if-exists < <arquivo.dump>
set -euo pipefail

PASTA="${PASTA:-/opt/crypto}"
valor() { grep "^$1=" "$PASTA/.env" | tail -1 | cut -d= -f2-; }
mkdir -p "$PASTA/backups"
arquivo="crypto-$(date -u +%Y%m%d-%H%M%S).dump"

docker exec crypto-prd-postgres pg_dump -U postgres -d crypto -Fc >"$PASTA/backups/$arquivo.parcial"
[ -s "$PASTA/backups/$arquivo.parcial" ] || { echo "pg_dump vazio" >&2; exit 1; }
mv "$PASTA/backups/$arquivo.parcial" "$PASTA/backups/$arquivo"
echo "  dump: $PASTA/backups/$arquivo ($(du -h "$PASTA/backups/$arquivo" | cut -f1))"
# shellcheck disable=SC2012
ls -1t "$PASTA"/backups/crypto-*.dump | tail -n +15 | xargs -r rm -f

repositorio="$(valor RESTIC_REPOSITORY)"
if [ -n "$repositorio" ]; then
  extra=()
  [ -f "$PASTA/backup.env" ] && extra=(--env-file "$PASTA/backup.env")
  restic() {
    docker run --rm "${extra[@]}" -e RESTIC_REPOSITORY="$repositorio" \
      -e RESTIC_PASSWORD="$(valor RESTIC_PASSWORD)" -v "$PASTA/backups:/backups:ro" \
      restic/restic:0.18.0 "$@"
  }
  restic cat config >/dev/null 2>&1 || restic init
  restic backup --tag crypto "/backups/$arquivo"
  restic forget --tag crypto --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune
fi
logger -t crypto-backup "ok $arquivo" 2>/dev/null || true
