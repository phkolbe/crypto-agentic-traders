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
# Vigia dois conteineres: o sistema (CONTEINER, crypto-prd) e o no Tailscale
# (CONTEINER_TS, crypto-prd-tailscale), o unico caminho ate as telas. O no fora do
# tailnet (chave expirada, logout) nao para a carteira, mas deixa o dono sem tela e sem
# acao; antes do healthcheck dele, ficava "Up" e ninguem sabia.
#
# O que faz, a cada execucao, para cada um (contagem e teto separados):
#   healthy   -> zera a contagem;
#   unhealthy -> conta; na VIGIA_LIMITE-esima seguida (padrao 3) reinicia o conteiner,
#                no maximo VIGIA_MAX_REINICIOS_HORA vezes por hora (padrao 3);
#   starting, parado ou ausente -> nao reinicia (subida em curso, ou parada pedida).
# E um ping so, com VIGIA_URL no .env:
#   sistema healthy e o no nao unhealthy     -> "<url>" (estou vivo);
#   qualquer um dos dois unhealthy           -> "<url>/fail" (convencao do healthchecks.io);
#   sistema starting/parado/ausente, no bem  -> nenhum.
# Dead-man's switch: o ping de vida so sai com o sistema saudavel. Se o host cair, o
# timer parar ou o conteiner nao voltar, o servico externo deixa de receber o ping e avisa.
# O no ausente (implantar.sh --local, ou antes da primeira subida dele) nao e falha.
#
# Variaveis (todas opcionais): PASTA (/opt/crypto), CONTEINER (crypto-prd), CONTEINER_TS
# (crypto-prd-tailscale), VIGIA_ESTADO (/var/lib/crypto-vigia, o StateDirectory do
# servico), VIGIA_LIMITE, VIGIA_MAX_REINICIOS_HORA. VIGIA_URL vem do $PASTA/.env; e
# segredo (quem a tem pinga em nome do sistema) e nunca e escrita no log.
set -uo pipefail

PASTA="${PASTA:-/opt/crypto}"
CONTEINER="${CONTEINER:-crypto-prd}"
CONTEINER_TS="${CONTEINER_TS:-crypto-prd-tailscale}"
ESTADO="${VIGIA_ESTADO:-/var/lib/crypto-vigia}"
LIMITE="${VIGIA_LIMITE:-3}"
MAX_HORA="${VIGIA_MAX_REINICIOS_HORA:-3}"

valor() { [ -r "$2" ] && grep "^$1=" "$2" | tail -1 | cut -d= -f2- ; }
URL="$(valor VIGIA_URL "$PASTA/.env" || true)"
mkdir -p "$ESTADO"

pingar() {
  [ -n "$URL" ] || return 0
  curl -fsS -m 10 --retry 2 -o /dev/null "$URL$1" || echo "vigia: ping falhou${1:+ ($1)}" >&2
}

# vigiar <conteiner> <prefixo dos arquivos de estado> <segundos do docker restart>
# Escreve o estado de saude em stdout; o resto vai para o stderr (journal).
vigiar() {
  local conteiner="$1" contagem="$ESTADO/${2}unhealthy-seguidos" reinicios="$ESTADO/${2}reinicios"
  local estado rodando seguidos motivo agora recentes feitos
  estado="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}sem-healthcheck{{end}}' \
    "$conteiner" 2>/dev/null || echo ausente)"
  rodando="$(docker inspect -f '{{.State.Running}}' "$conteiner" 2>/dev/null || echo false)"
  case "$estado" in
    healthy)
      echo 0 >"$contagem"
      ;;
    unhealthy)
      seguidos=$(( $(cat "$contagem" 2>/dev/null || echo 0) + 1 ))
      echo "$seguidos" >"$contagem"
      motivo="$(docker inspect -f '{{with .State.Health}}{{range .Log}}{{.Output}}{{end}}{{end}}' \
        "$conteiner" 2>/dev/null | tail -c 400 | tr '\n' ' ')"
      echo "vigia: $conteiner unhealthy ($seguidos/$LIMITE seguidas): $motivo" >&2
      if [ "$seguidos" -ge "$LIMITE" ]; then
        agora="$(date +%s)"
        # So os reinicios da ultima hora contam para o teto.
        recentes="$(awk -v corte=$((agora - 3600)) '$1 > corte' "$reinicios" 2>/dev/null || true)"
        if [ -n "$recentes" ]; then printf '%s\n' "$recentes" >"$reinicios"; else : >"$reinicios"; fi
        feitos="$(grep -c . "$reinicios" 2>/dev/null || echo 0)"
        if [ "$feitos" -ge "$MAX_HORA" ]; then
          # Reiniciar em laco nao conserta o que nao volta (a mesma licao do watchdog, D25):
          # para de reiniciar e deixa o dead-man avisar -- o ping de saude nao sai mais.
          echo "vigia: $conteiner: $feitos reinicios na ultima hora; nao reinicia mais, precisa de alguem" >&2
        else
          echo "vigia: reiniciando $conteiner" >&2
          if docker restart -t "$3" "$conteiner" >/dev/null; then
            echo "$agora" >>"$reinicios"
            echo 0 >"$contagem"
          else
            echo "vigia: docker restart $conteiner falhou" >&2
          fi
        fi
      fi
      ;;
    *)
      # starting: a subida tem o start-period do HEALTHCHECK. Parado/ausente: o Docker
      # reinicia o que saiu sozinho (restart: unless-stopped); o que foi parado de
      # proposito (docker stop, implantar.sh, restaurar.sh) nao e desfeito aqui.
      echo "vigia: $conteiner $estado (rodando=$rodando); nada a fazer" >&2
      ;;
  esac
  echo "$estado"
}

# O sistema guarda os nomes de arquivo de antes (sem prefixo): a contagem em curso
# sobrevive a atualizacao do script. 60 s: a parada do crypto drena o evento em curso.
sistema="$(vigiar "$CONTEINER" "" 60)"
no="$(vigiar "$CONTEINER_TS" "tailscale-" 10)"

if [ "$sistema" = unhealthy ] || [ "$no" = unhealthy ]; then
  pingar "/fail"
elif [ "$sistema" = healthy ]; then
  pingar ""
fi
exit 0
