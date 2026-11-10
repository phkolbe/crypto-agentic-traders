#!/usr/bin/env bash
# Backup do banco do Sistema Cripto em producao: `pg_dump` (formato custom) em
# /opt/crypto/backups, guardando os 14 mais recentes, e, com RESTIC_REPOSITORY no .env,
# uma copia cifrada no repositorio restic do Crypto (7 diarios, 4 semanais, 6 mensais).
# Credenciais do provedor do restic (AWS_*, B2_*...) em /opt/crypto/backup.env (modo 600).
#
#     sudo implantacao/producao/backup.sh       # tambem pelo timer do systemd, 03:30
#
# A copia FORA do servidor (restic) e obrigatoria antes do live: os 14 dumps locais moram
# no mesmo disco da VM, e perder a VM perderia o historico de negociacoes (base do preco
# medio do stop e da apuracao fiscal). Sem RESTIC_REPOSITORY o backup roda, mas avisa no
# journal a cada noite.
#
# Falha: o servico do systemd tem OnFailure=crypto-falha@ (avisar-falha.sh), que loga no
# journal com prioridade de erro e pinga VIGIA_URL/fail. Antes, um pg_dump que falhava toda
# noite (disco cheio, conteiner renomeado) so deixava rastro no journal.
#
# Restaurar e ensaiar a restauracao: restaurar.sh (README, "Backup e restauracao").
set -euo pipefail

# Dumps 0600 e pasta 0700: o dump leva historico de negociacoes, saldos e os destinatarios
# dos alertas. Rodando como root com o umask padrao (022) eles saiam 0644, legiveis por
# qualquer usuario ou sistema da maquina.
umask 077

PASTA="${PASTA:-/opt/crypto}"
valor() { grep "^$1=" "$PASTA/.env" | tail -1 | cut -d= -f2-; }
mkdir -p "$PASTA/backups"
chmod 700 "$PASTA/backups"
arquivo="crypto-$(date -u +%Y%m%d-%H%M%S).dump"
parcial="$PASTA/backups/$arquivo.parcial"
# Restos de falhas anteriores nao entram na rotacao (padrao *.dump): limpar aqui.
rm -f "$PASTA"/backups/crypto-*.dump.parcial
trap 'rm -f "$parcial"' EXIT

docker exec crypto-prd-postgres pg_dump -U postgres -d crypto -Fc >"$parcial"
[ -s "$parcial" ] || { echo "pg_dump vazio" >&2; exit 1; }
mv "$parcial" "$PASTA/backups/$arquivo"
trap - EXIT
# Dumps de antes do umask (0644) tambem fecham.
chmod 600 "$PASTA"/backups/crypto-*.dump
echo "  dump: $PASTA/backups/$arquivo ($(du -h "$PASTA/backups/$arquivo" | cut -f1))"
# shellcheck disable=SC2012
ls -1t "$PASTA"/backups/crypto-*.dump | tail -n +15 | xargs -r rm -f

repositorio="$(valor RESTIC_REPOSITORY || true)"
if [ -n "$repositorio" ]; then
  extra=()
  if [ -f "$PASTA/backup.env" ]; then
    chmod 600 "$PASTA/backup.env"
    extra=(--env-file "$PASTA/backup.env")
  fi
  # A senha vai por ARQUIVO (RESTIC_PASSWORD_FILE): em `-e RESTIC_PASSWORD=...` ela ficava
  # visivel no `ps` do host e no `docker inspect` enquanto o backup rodava.
  segredos="$(mktemp -d)"
  trap 'rm -rf "$segredos"' EXIT
  valor RESTIC_PASSWORD >"$segredos/senha" || true
  # restic 0.18.0, fixado por digest (le a senha e as credenciais do provedor): README,
  # "Imagens".
  restic() {
    docker run --rm "${extra[@]}" -e RESTIC_REPOSITORY="$repositorio" \
      -e RESTIC_PASSWORD_FILE=/run/restic/senha -v "$segredos:/run/restic:ro" \
      -v "$PASTA/backups:/backups:ro" \
      restic/restic:0.18.0@sha256:4cf4a61ef9786f4de53e9de8c8f5c040f33830eb0a10bf3d614410ee2fcb6120 "$@"
  }
  restic cat config >/dev/null 2>&1 || restic init
  restic backup --tag crypto "/backups/$arquivo"
  restic forget --tag crypto --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune
else
  aviso="sem copia fora do servidor: RESTIC_REPOSITORY vazio em $PASTA/.env (README, Backup e restauracao)"
  echo "AVISO: $aviso" >&2
  logger -t crypto-backup -p user.warning "$aviso" 2>/dev/null || true
fi
logger -t crypto-backup "ok $arquivo" 2>/dev/null || true
