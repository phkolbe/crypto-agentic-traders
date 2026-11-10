#!/usr/bin/env bash
# Vigia do Sistema Cripto, de FORA do processo (systemd/crypto-vigia.timer, a cada 1 min).
#
#     sudo implantacao/producao/vigia.sh        # o que o timer roda
#
# Por que existe: o Docker sem Swarm NAO reinicia conteiner `unhealthy` -- o `restart:
# unless-stopped` so age quando o processo SAI. Com o laco de eventos travado ou os
# agentes mortos, o HEALTHCHECK (`/api/health?sonda=1`) fica vermelho para sempre, os
# alertas (que saem do mesmo processo) nao saem, e o stop -- que e so software -- nao
# roda ate alguem abrir a tela.
#
# O que faz, a cada execucao:
#   healthy   -> zera a contagem e, com VIGIA_URL no .env, pinga "estou vivo";
#   unhealthy -> conta; na VIGIA_LIMITE-esima seguida (padrao 3) reinicia o conteiner,
#                no maximo VIGIA_MAX_REINICIOS_HORA vezes por hora (padrao 3); com
#                VIGIA_URL, pinga "<url>/fail" (convencao do healthchecks.io);
#   starting, parado ou ausente -> nao reinicia (subida em curso, ou parada pedida) e
#                nao pinga.
# Dead-man's switch: o ping so sai com o sistema saudavel. Se o host cair, o timer
# parar ou o conteiner nao voltar, o servico externo deixa de receber o ping e avisa.
#
# Variaveis (todas opcionais): PASTA (/opt/crypto), CONTEINER (crypto-prd), VIGIA_ESTADO
# (/var/lib/crypto-vigia, o StateDirectory do servico), VIGIA_LIMITE,
# VIGIA_MAX_REINICIOS_HORA. VIGIA_URL vem do $PASTA/.env; e segredo (quem a tem pinga em
# nome do sistema) e nunca e escrita no log.
set -uo pipefail

PASTA="${PASTA:-/opt/crypto}"
CONTEINER="${CONTEINER:-crypto-prd}"
ESTADO="${VIGIA_ESTADO:-/var/lib/crypto-vigia}"
LIMITE="${VIGIA_LIMITE:-3}"
MAX_HORA="${VIGIA_MAX_REINICIOS_HORA:-3}"

valor() { [ -r "$2" ] && grep "^$1=" "$2" | tail -1 | cut -d= -f2- ; }
URL="$(valor VIGIA_URL "$PASTA/.env" || true)"
mkdir -p "$ESTADO"
CONTAGEM="$ESTADO/unhealthy-seguidos"
REINICIOS="$ESTADO/reinicios"

pingar() {
  [ -n "$URL" ] || return 0
  curl -fsS -m 10 --retry 2 -o /dev/null "$URL$1" || echo "vigia: ping falhou${1:+ ($1)}" >&2
}

estado="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}sem-healthcheck{{end}}' \
  "$CONTEINER" 2>/dev/null || echo ausente)"
rodando="$(docker inspect -f '{{.State.Running}}' "$CONTEINER" 2>/dev/null || echo false)"

case "$estado" in
  healthy)
    echo 0 >"$CONTAGEM"
    pingar ""
    ;;
  unhealthy)
    seguidos=$(( $(cat "$CONTAGEM" 2>/dev/null || echo 0) + 1 ))
    echo "$seguidos" >"$CONTAGEM"
    motivo="$(docker inspect -f '{{with .State.Health}}{{range .Log}}{{.Output}}{{end}}{{end}}' \
      "$CONTEINER" 2>/dev/null | tail -c 400 | tr '\n' ' ')"
    echo "vigia: $CONTEINER unhealthy ($seguidos/$LIMITE seguidas): $motivo" >&2
    pingar "/fail"
    if [ "$seguidos" -ge "$LIMITE" ]; then
      agora="$(date +%s)"
      # So os reinicios da ultima hora contam para o teto.
      recentes="$(awk -v corte=$((agora - 3600)) '$1 > corte' "$REINICIOS" 2>/dev/null || true)"
      if [ -n "$recentes" ]; then printf '%s\n' "$recentes" >"$REINICIOS"; else : >"$REINICIOS"; fi
      feitos="$(grep -c . "$REINICIOS" 2>/dev/null || echo 0)"
      if [ "$feitos" -ge "$MAX_HORA" ]; then
        # Reiniciar em laco nao conserta o que nao volta (a mesma licao do watchdog, D25):
        # para de reiniciar e deixa o dead-man avisar -- o ping de saude nao sai mais.
        echo "vigia: $feitos reinicios na ultima hora; nao reinicia mais, precisa de alguem" >&2
      else
        echo "vigia: reiniciando $CONTEINER" >&2
        if docker restart -t 60 "$CONTEINER" >/dev/null; then
          echo "$agora" >>"$REINICIOS"
          echo 0 >"$CONTAGEM"
        else
          echo "vigia: docker restart falhou" >&2
        fi
      fi
    fi
    ;;
  *)
    # starting: a subida tem o start-period do HEALTHCHECK. Parado/ausente: o Docker
    # reinicia o que saiu sozinho (restart: unless-stopped); o que foi parado de
    # proposito (docker stop, implantar.sh) nao e desfeito aqui.
    echo "vigia: $CONTEINER $estado (rodando=$rodando); nada a fazer" >&2
    ;;
esac
exit 0
