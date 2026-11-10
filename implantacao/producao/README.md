# Produção do Sistema Cripto

Uma stack só do Crypto (projeto `crypto-prd`), isolada de qualquer outro sistema que more na mesma máquina: rede,
Postgres, nó Tailscale e backup próprios. Desenho e decisões no cofre Obsidian `cryptocoins` (`implantacao.md`,
D32–D39); roteiro completo, inclusive ligar o `live`, em `acessos-e-operacao.md`.

| Arquivo | O que é |
|---|---|
| `compose.yml` | `tailscale` (nó `crypto`, `tailscale serve` no 443), `postgres` (16), `migrar` (roda e sai) e `crypto` |
| `serve.json` | HTTPS do nó → `http://crypto-prd:8000` |
| `preparar.sh` | cria `/opt/crypto/.env` (senhas novas) e `/opt/crypto/crypto.env` (nasce em `dry_run`), modo 600 |
| `implantar.sh` | imagens → Postgres → backup → `migrar` → sistema saudável → confere a trava → nó Tailscale |
| `diagnostico.sh` | `crypto-traders check` num contêiner próprio e descartável — **nunca** `docker exec crypto-prd crypto-traders check` (o diagnóstico divide os 512 MB do sistema e o kernel mata o contêiner, com o stop junto; o comando recusa rodar ali) |
| `backup.sh`, `systemd/` | `pg_dump` diário (14 locais) e restic opcional; timer 03:30 |
| `vigia.sh`, `systemd/crypto-vigia.*` | vigia de fora do processo, a cada minuto: reinicia o `crypto-prd` `unhealthy` e pinga o `VIGIA_URL` (opcional) enquanto ele está saudável |
| `env.exemplo`, `crypto.env.exemplo` | o que cada arquivo da pasta leva |

## Primeira vez, no servidor

```bash
sudo git clone https://github.com/phkolbe/crypto-agentic-traders /opt/crypto/codigo
cd /opt/crypto/codigo
sudo implantacao/producao/preparar.sh --tailnet <rede>.ts.net --login <seu e-mail no Tailscale>
sudo nano /opt/crypto/.env       # VERSAO_CRYPTO, TS_AUTHKEY (só agora), OTEL_ENDPOINT, restic, VIGIA_URL
sudo implantacao/producao/implantar.sh --sem-backup
sudo cp implantacao/producao/systemd/crypto-backup.* implantacao/producao/systemd/crypto-vigia.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-backup.timer crypto-vigia.timer
```

Depois: apagar o `TS_AUTHKEY` do `.env` e desligar a expiração da chave do nó `crypto` no painel do Tailscale.
Endereço: `https://crypto.<tailnet>.ts.net/crypto-traders/` (a raiz redireciona para lá), só para `CRYPTO_LOGIN` (a API recusa os outros com 403).

## Vigia de fora do processo

O HEALTHCHECK da imagem pergunta `/crypto-traders/api/health?sonda=1`, que responde 503 quando o sistema não está
protegendo a carteira: `market_data`, `risk_manager`, `execution` ou `portfolio` sem batida há mais de 10 min, com a
tarefa parada ou desistidos pelo watchdog, ou o laço do watchdog sem completar um ciclo há mais de 3 min. Pausa pedida
pela interface ou pelo circuit breaker não conta como doença. Sem o parâmetro a rota continua 200 (a tela a lê).

O Docker **não reinicia** contêiner `unhealthy` (o `restart: unless-stopped` só age quando o processo sai). Quem
reinicia é o `crypto-vigia.timer`: a cada minuto o `vigia.sh` lê o estado de saúde do `crypto-prd` e

- `healthy`: zera a contagem e pinga o `VIGIA_URL`;
- `unhealthy`: na 3ª verificação seguida faz `docker restart -t 60 crypto-prd`, no máximo 3 vezes por hora (depois
  para de reiniciar e espera alguém); pinga `VIGIA_URL/fail`;
- `starting`, parado ou ausente: não faz nada (subida em curso, ou parada pedida — `docker stop` não é desfeito).

`VIGIA_URL` é opcional e é o *dead-man's switch*: um serviço externo (ex.: um check do healthchecks.io, período
1 min e tolerância de uns 5 min) que avisa quando o ping **para** de chegar — host fora do ar, timer parado ou
contêiner que não volta. A conta no serviço é do dono; aqui só se lê a URL do `/opt/crypto/.env`. Durante uma
manutenção longa, pausar o check no serviço para não receber o alarme. Log: `journalctl -u crypto-vigia`.

## Acesso

A API não tem login próprio. Aqui ela só é alcançável pelo `tailscale serve`, que põe em cada pedido o
`Tailscale-User-Login` de quem acessa (e sobrescreve o que o cliente mandar); com `ACESSO_LOGIN_TAILSCALE` definido,
a API (`api.app.PortaoTailscale`) recusa qualquer outro login, em HTTP e WebSocket. Nenhuma porta é publicada no
servidor.

## Teste na máquina de desenvolvimento

```bash
docker compose build crypto
PASTA=C:/algum/lugar implantacao/producao/preparar.sh --tailnet exemplo.ts.net --login voce@exemplo.com
sed -i 's/^VERSAO_CRYPTO=$/VERSAO_CRYPTO=local/' C:/algum/lugar/.env
PASTA=C:/algum/lugar implantacao/producao/implantar.sh --local --sem-backup
```

`--local` não baixa imagens nem sobe o nó Tailscale. Desfazer:
`docker compose -p crypto-prd --env-file C:/algum/lugar/.env -f implantacao/producao/compose.yml down -v`.
