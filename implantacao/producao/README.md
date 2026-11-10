# Produção do Sistema Cripto

Uma stack só do Crypto (projeto `crypto-prd`), isolada de qualquer outro sistema que more na mesma máquina: rede,
Postgres, nó Tailscale e backup próprios. Desenho e decisões no cofre Obsidian `cryptocoins` (`implantacao.md`,
D32–D39); roteiro completo, inclusive ligar o `live`, em `acessos-e-operacao.md`.

| Arquivo | O que é |
|---|---|
| `compose.yml` | `tailscale` (nó `crypto`, `tailscale serve` no 443), `postgres` (16), `migrar` (roda e sai) e `crypto` |
| `serve.json` | HTTPS do nó → `http://crypto-prd:8000` |
| `preparar.sh` | cria `/opt/crypto/.env` (senhas novas), `/opt/crypto/crypto.env` (nasce em `dry_run`) e `backup.env`, modo 600; `backups/` em 700 |
| `implantar.sh` | imagens → Postgres → backup → `migrar` → sistema saudável → confere a trava → nó Tailscale |
| `diagnostico.sh` | `crypto-traders check` num contêiner próprio e descartável — **nunca** `docker exec crypto-prd crypto-traders check` (o diagnóstico divide os 512 MB do sistema e o kernel mata o contêiner, com o stop junto; o comando recusa rodar ali) |
| `backup.sh`, `systemd/crypto-backup.*` | `pg_dump` diário (14 locais, 0600) e cópia fora do servidor com restic; timer 03:30 |
| `avisar-falha.sh`, `systemd/crypto-falha@.service` | aviso de unidade que falhou (`OnFailure=` do backup): journal com prioridade de erro e ping em `VIGIA_URL/fail` |
| `restaurar.sh` | ensaio de restauração (`--ensaio`, num database descartável) e a restauração de verdade (`--confirmar`) |
| `vigia.sh`, `systemd/crypto-vigia.*` | vigia de fora do processo, a cada minuto: reinicia o `crypto-prd` `unhealthy` e pinga o `VIGIA_URL` (opcional) enquanto ele está saudável |
| `env.exemplo`, `crypto.env.exemplo` | o que cada arquivo da pasta leva |

## Primeira vez, no servidor

```bash
sudo git clone https://github.com/phkolbe/crypto-agentic-traders /opt/crypto/codigo
cd /opt/crypto/codigo
sudo implantacao/producao/preparar.sh --tailnet <rede>.ts.net --login <seu e-mail no Tailscale>
sudo nano /opt/crypto/.env       # VERSAO_CRYPTO, TS_AUTHKEY (só agora), OTEL_ENDPOINT, RESTIC_REPOSITORY, VIGIA_URL
sudo nano /opt/crypto/backup.env # credenciais do provedor do restic
sudo implantacao/producao/implantar.sh --sem-backup
sudo cp implantacao/producao/systemd/crypto-backup.* implantacao/producao/systemd/crypto-vigia.* \
  implantacao/producao/systemd/crypto-falha@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-backup.timer crypto-vigia.timer
sudo systemctl start crypto-backup.service          # primeiro backup (e a primeira cópia fora)
sudo implantacao/producao/restaurar.sh --ensaio      # prova que o dump volta
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

## Backup e restauração

- **Todo dia, 03:30** (`crypto-backup.timer`): `pg_dump` em `/opt/crypto/backups` (os 14 mais recentes; arquivos
  0600, pasta 0700) e, com `RESTIC_REPOSITORY`, cópia cifrada no repositório restic do Crypto (7 diários,
  4 semanais, 6 mensais). A senha do restic vai por arquivo, nunca na linha de comando.
- **Cópia fora do servidor — obrigatória antes do `live`.** Os dumps locais moram no mesmo disco da VM; perder a VM
  sem a cópia perde o histórico de negociações (base do preço médio do stop e da apuração fiscal). Passos: um
  bucket/repositório só do Crypto num provedor (S3, B2...), `RESTIC_REPOSITORY` no `.env`, credenciais no
  `backup.env`, e uma cópia da `RESTIC_PASSWORD` (gerada pelo `preparar.sh`) no gerenciador de senhas. Sem o
  repositório o backup roda só local e deixa um aviso no journal toda noite.
- **Falha não é silenciosa**: o serviço tem `OnFailure=crypto-falha@%n.service`, que loga com prioridade de erro
  (`journalctl -p err -t crypto-falha`) e, com `VIGIA_URL`, pinga `VIGIA_URL/fail` com o fim do log da unidade. No
  healthchecks.io isso põe o check em "down" e avisa o dono; o ping seguinte do vigia (1 min) o devolve a "up" — o
  aviso é o "down". Não é o e-mail do próprio sistema de propósito: o backup falha justamente quando o Postgres ou o
  disco estão com problema.
- **Ensaio de restauração — todo mês e depois de mudar o schema**: `sudo implantacao/producao/restaurar.sh --ensaio`
  restaura o dump mais recente (ou o arquivo dado) num database descartável `crypto_ensaio` do mesmo Postgres,
  confere que as tabelas e os gatilhos append-only são os do `crypto`, lista as linhas por tabela e apaga o
  descartável. O banco `crypto` só é lido. Para ensaiar a cópia de fora (a que importa se a VM se perder), trazer
  um dump do restic para um arquivo local (`restic restore`) e passar o caminho a `restaurar.sh --ensaio`.
- **Restaurar de verdade**: `sudo implantacao/producao/restaurar.sh --confirmar <arquivo.dump>`. Guarda o estado
  atual em `backups/antes-de-restaurar-*.dump`, para o `crypto-prd` e restaura com
  `pg_restore --clean --if-exists --single-transaction` (erro no meio desfaz tudo; o banco fica como estava). O
  sistema fica **parado**: em `live`, o banco voltou para a data do dump e as ordens e trades posteriores existem só
  na Binance — conferir e lançar o que faltar antes de religar com `implantar.sh`. Servidor novo: `implantar.sh
  --sem-backup` primeiro (cria o volume, os usuários e o `GRANT CONNECT`, que o dump não traz), depois restaurar.

## Acesso

A API não tem login próprio. O caminho de fora é o `tailscale serve`, que põe em cada pedido o
`Tailscale-User-Login` de quem acessa (e sobrescreve o que o cliente mandar); com `ACESSO_LOGIN_TAILSCALE` definido,
a API (`api.app.PortaoTailscale`) recusa qualquer outro login, em HTTP e WebSocket. Nenhuma porta é publicada no
servidor.

A API escuta em `0.0.0.0:8000` na rede `crypto-prd`: o host e os outros contêineres da rede chegam a ela pelo IP da
bridge, sem passar pelo serve, e podiam mandar o cabeçalho com o login do dono. Por isso o cabeçalho só vale quando a
conexão vem do contêiner `crypto-prd-tailscale` (`ACESSO_PROXY_TAILSCALE`, resolvido pelo DNS da rede e refeito se o
contêiner for recriado com outro IP), e os outros contêineres não têm `NET_RAW` (sem ele não se forja pacote com o IP
de outro contêiner na bridge). O `implantar.sh` confere as duas coisas: sem o cabeçalho e com o login forjado de
dentro do contêiner, 403. Com `ACESSO_LOGIN_TAILSCALE` definido, a trava de origem aceita só a origem das telas
(`CORS_ORIGINS`, o endereço `ts.net`): página em `localhost` no aparelho do dono não aciona rota com efeito.

Por que não `network_mode: service:tailscale` com a API em `127.0.0.1`: o `crypto` passaria a depender do
namespace de rede do contêiner do Tailscale (128 MB); um reinício dele deixa o `crypto` sem rede — Postgres e
Binance incluídos — até ser recriado, e o pedido do serve e a sonda de saúde passariam a vir do mesmo 127.0.0.1.

## Teste na máquina de desenvolvimento

```bash
docker compose build crypto
PASTA=C:/algum/lugar implantacao/producao/preparar.sh --tailnet exemplo.ts.net --login voce@exemplo.com
sed -i 's/^VERSAO_CRYPTO=$/VERSAO_CRYPTO=local/' C:/algum/lugar/.env
PASTA=C:/algum/lugar implantacao/producao/implantar.sh --local --sem-backup
```

`--local` não baixa imagens nem sobe o nó Tailscale. Desfazer:
`docker compose -p crypto-prd --env-file C:/algum/lugar/.env -f implantacao/producao/compose.yml down -v`.
