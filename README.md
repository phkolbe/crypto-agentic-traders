# crypto-agentic-traders

Agentes autônomos de negociação de criptomoedas na **Binance**, com **Risk
Manager como guardião obrigatório** de toda ordem, backend em Python e dashboard
em React + TypeScript.

> **O padrão de fábrica é simulação.** Sem nenhuma configuração, o sistema roda
> em `DRY_RUN`: nenhuma ordem sai da máquina. Operar com dinheiro real exige duas
> confirmações explícitas no `.env` (ver [Modos de operação](#modos-de-operação)).

---

## Arquitetura

```
Market Data ──► Strategy ──► Risk Manager ──► Execution ──► Portfolio
   Agent          Agent         (guardião)       Agent         Agent
     │              │               │              │             │
     └──────────────┴───── Event Bus ─────────────┴─────────────┘
                              │
                   Banco de dados + Audit Log
                              │
                     FastAPI  ──►  React + TS
```

Cada agente tem uma responsabilidade única e se comunica apenas pelo event bus.
A regra estrutural do sistema: **o Strategy Agent nunca executa ordens, e o
Execution Agent nunca decide** — entre os dois há sempre o Risk Manager, que
registra em banco toda decisão, aprovada ou rejeitada.

O diagrama esconde o caminho que mais importa: **o Portfolio Agent volta ao Risk
Manager.** A cada snapshot (60s) ele alimenta a comparação de stop-loss e a
reavaliação do circuit breaker — a proteção é um laço, não uma etapa da esteira.

Detalhamento em [`docs/ARQUITETURA.md`](docs/ARQUITETURA.md).

### Estado atual, sem otimismo

- Roda em `dry_run`. **Nunca operou em `live`**: não há uma ordem de dinheiro real
  no histórico, e todo número de retorno neste repositório vem de backtest.
- Suíte verde em **1118 passed, 6 skipped, 1 xfailed** — e o que os skips e o
  xfail escondem está em [Os limites conhecidos](#os-limites-conhecidos-em-uma-tela).
- O processo **não sobrevive** à suspensão da máquina. Ele detecta e alerta que
  morreu na subida seguinte, o que não é a mesma coisa que continuar rodando.

### Onde está o quê

```
backend/src/crypto_traders/
├── agents/          um arquivo por agente + base.py (ciclo de vida, heartbeat,
│                    caixa de entrada durável) e orchestrator.py (watchdog,
│                    pausa/bloqueio, tarefas próprias)
├── risk/rules.py    RiskEngine: a lógica pura de risco, sem I/O
├── db/              models.py (schema, gatilhos, autorizador do driver),
│                    repositories.py, session.py
├── exchanges/       Broker (com credencial) e MarketDataSource (sem)
├── strategies/ indicators/    cálculo próprio, com teste contra referência
├── backtest/        dois motores, reusando Strategy, RiskEngine e PaperBroker
└── api/             FastAPI + WebSocket
backend/tests/       35 arquivos; os `test_critico_*` e `test_ataque_*` são os
                     grupos de contraprova do endurecimento
frontend/src/        telas, format.ts (funções puras) e checks/telas.check.ts
```

Se você vai mexer em uma proteção, leia **antes**
[`docs/SEGURANCA.md`](docs/SEGURANCA.md) §12 (escopo do stop) e §17 (o que não
foi provado). As duas existem porque este projeto já perdeu proteção por
documentação otimista.

## Escopo atual

Opera **somente na Binance**. A Coinbase está na fase 5 do roadmap — o adapter
`ccxt` já é genérico, então adicioná-la depois não mexe em código de agente.

## Requisitos

- Python 3.13+ (testado em 3.14)
- Node.js 20+ (para o dashboard)
- Docker é **opcional** — veja [Persistência](#persistência)

## Instalação

```bash
uv venv --python 3.14
uv pip install -e ".[dev]"
cp .env.example .env
```

Frontend:

```bash
cd frontend && npm install
```

## Como rodar

Backend (agentes + API):

```bash
uv run crypto-traders run
```

Dashboard:

```bash
cd frontend && npm run dev
```

O dashboard sobe em `http://localhost:5173` e a API em `http://127.0.0.1:8000`
(apenas localhost — a porta nunca é exposta na rede).

Outros comandos:

```bash
uv run crypto-traders check
```

```bash
uv run crypto-traders backtest --symbol BTC/USDT --strategy ma_crossover --days 90
```

## Configuração: ambiente e negócio moram em lugares diferentes

Esta separação é uma regra do projeto, não uma preferência de organização.

| | **Ambiente** | **Negócio** |
|---|---|---|
| **Onde** | `.env` no servidor | banco de dados |
| **Como editar** | editar o arquivo e reiniciar | telas **Configurações** e **Risco** |
| **O quê** | credenciais, banco, event bus, host/porta da API, CORS, log, SMTP/WhatsApp, `EXCHANGE`, `TRADING_MODE` | pares, moeda de cotação, timeframe, estratégias, cadência, limites de risco, filtro MVRV, parâmetros de simulação |
| **Rastro** | nenhum | `audit_log`, com antes e depois |

Escrever uma variável de negócio no `.env` faz o sistema **recusar subir**,
listando as chaves. Isso é proposital: antes dessa trava, o `.env` pedia ordem
máxima de 7 USDT durante dias enquanto o sistema operava com 50 — o arquivo tinha
semeado o banco na primeira subida e nunca mais fora lido. Um valor que parece ter
efeito e não tem é pior que valor nenhum.

Duas fronteiras que não são óbvias, e por quê:

- **`TRADING_MODE` é ambiente.** Ligar dinheiro real exige editar um arquivo no
  servidor e reiniciar o processo — duas travas que um clique no navegador não
  alcança.
- **`EXCHANGE` é ambiente, mas a moeda de cotação é negócio.** A exchange está
  amarrada a qual credencial existe no arquivo; a moeda define o universo de
  pares negociáveis.

## Pares negociados

Em **Configurações**, o campo de pares aceita duas formas:

- **lista preenchida** — negocia exatamente esses pares;
- **lista vazia** — descoberta automática ("mar aberto"): o sistema varre a
  exchange e escolhe os pares mais líquidos sozinho.

No modo automático a lista descoberta vira a **whitelist efetiva** do Risk
Manager, registrada no `audit_log` a cada mudança.

> Leia [`docs/SEGURANCA.md`](docs/SEGURANCA.md) antes de usar o modo automático:
> a whitelist deixa de ser uma lista aprovada por você e passa a ser um conjunto
> de critérios, o que torna o **máximo de posições abertas** e a exposição máxima
> por ativo as principais defesas.

## Alertas

E-mail (SMTP) e WhatsApp (Meta Cloud API), cada um com liga/desliga na tela
**Notificações**. Segredos ficam no `.env`; destinatários e toggles no banco.

## Modos de operação

| `TRADING_MODE` | Ordens saem da máquina? | Dinheiro | Quando usar |
|---|---|---|---|
| `dry_run` *(padrão)* | Não | Simulado | Sempre, no início |
| `testnet` | Sim, para a testnet | Fictício | Validar integração real com a exchange |
| `live` | **Sim** | **Real** | Só após semanas de paper trading |

`live` exige **duas** variáveis, propositalmente:

```dotenv
TRADING_MODE=live
LIVE_TRADING_CONFIRMED=true
```

Um caractere trocado no `.env` não é suficiente para começar a gastar dinheiro.

## Persistência

Por padrão o sistema usa **SQLite** (arquivo em `data/`) e um **event bus
in-process**, sem exigir Docker — funciona em qualquer máquina com Python.

Para migrar a PostgreSQL/TimescaleDB + Redis Streams, suba o compose e troque
duas linhas no `.env` — nenhum código de agente muda:

```bash
docker compose up -d
```

```dotenv
DATABASE_URL=postgresql+asyncpg://crypto:crypto@localhost:5432/crypto_traders
EVENT_BUS=redis
```

```bash
uv pip install -e ".[dev,postgres,redis]"
```

> ⚠️ **Essa migração nunca foi executada nesta máquina.** Não há Docker,
> PostgreSQL nem Redis aqui, e os 6 testes de Postgres da suíte aparecem como
> `skipped`. O schema, os gatilhos de append-only e as *hypertables* do Timescale
> estão implementados e **não exercitados** — "trocar duas linhas" descreve o
> desenho, não uma migração observada.

## Segurança

Resumo do que o sistema garante estruturalmente:

- Chaves de API **nunca** em código, banco ou arquivo versionado — só no `.env`
  (que está no `.gitignore`), carregadas como `SecretStr` e redigidas no log.
- Crie as chaves **sem permissão de saque** e com **whitelist de IP**.
- Toda ordem passa pelo Risk Manager: limite por operação, whitelist de ativos,
  exposição máxima por ativo, cooldown, e stop-loss/take-profit anexados a toda
  abertura — nenhuma abertura sem stop sai do Execution Agent.
- **O stop é executado em software**, pelo Risk Manager a cada snapshot, e **não
  pela exchange**: nenhuma OCO é enviada. Leia §12 antes de assumir cobertura.
- Circuit breaker de perda diária/semanal **bloqueia a abertura** de posição e
  **só rearma manualmente**. Nunca bloqueia o fechamento: pausar quem executa o
  stop desligaria o stop.
- Idempotência por `client_order_id`: um retry de rede não duplica ordem.
- `audit_log` append-only, com gatilho no banco e autorizador no driver.
- API escuta apenas em `127.0.0.1`.

Detalhamento em [`docs/SEGURANCA.md`](docs/SEGURANCA.md).

### Os limites conhecidos, em uma tela

Um engenheiro novo deve sair desta seção sabendo o que o sistema **não** garante:

| Limite | Onde está escrito |
|---|---|
| **Seis** situações em que uma posição aberta fica sem stop — inclusive posição sem preço médio e poeira abaixo do mínimo da exchange | [`SEGURANCA.md` §12](docs/SEGURANCA.md) |
| PostgreSQL/TimescaleDB **nunca rodaram** aqui; os 6 testes deles são `skipped` | [`SEGURANCA.md` §17](docs/SEGURANCA.md) |
| **Nunca houve teste de integração contra a testnet.** Nenhuma ordem deste sistema chegou a um servidor da Binance | [`SEGURANCA.md` §17](docs/SEGURANCA.md) |
| O processo **não sobrevive** à suspensão da máquina — "deixa rodando alguns dias" ainda não é executável | [`SEGURANCA.md` §17](docs/SEGURANCA.md) |
| Aporte menor que a perda ainda mascara parte dela no circuit breaker (`xfail` estrito) | [`SEGURANCA.md` §17](docs/SEGURANCA.md) |
| A trilha append-only **não** resiste a código hostil dentro do próprio processo | [`SEGURANCA.md` §5](docs/SEGURANCA.md) |
| Três filtros de regime medidos e desligados; nenhum replicou fora da janela | [`SEGURANCA.md` §11](docs/SEGURANCA.md) |
| **Nunca operou em `live`.** Todo número de retorno vem de backtest | [`SEGURANCA.md` §17](docs/SEGURANCA.md) |

## Testes

> ⚠️ **Rode de dentro de `backend/`.** Um teste do grupo de ataques lê arquivos
> de `src/` por caminho relativo, então a suíte só passa com esse diretório de
> trabalho. Da raiz do repositório, 1 dos 1.118 falha com `FileNotFoundError`.

```bash
cd backend && uv run pytest -q
```

Verde hoje: **1118 passed, 6 skipped, 1 xfailed**. Os três números importam:

- os **6 skips** são os testes de PostgreSQL/TimescaleDB, condicionados a
  `POSTGRES_TEST_URL`. Não há servidor nesta máquina, então o schema de Postgres
  está implementado e **não exercitado**;
- o **1 xfail** é `strict=True` com motivo escrito. É limitação documentada, não
  teste adiado — o caso do aporte menor que a perda no circuit breaker
  ([`SEGURANCA.md` §17](docs/SEGURANCA.md)).

Lint e frontend:

```bash
cd backend && uv run ruff check src tests
cd frontend && npx tsc -b && npx vite build && npm run check
```

`npm run check` roda 174 verificações de tela (`src/checks/telas.check.ts`) que
chamam as funções puras de formatação com os dados reais do sistema — existem
porque `tsc` passa limpo com a tela errada na frente do usuário.

O Risk Manager é o componente com cobertura mais alta — é ele que separa
"autônomo" de "descontrolado".

### A disciplina de teste deste projeto

A suíte foi de 419 para 1.118 testes numa rodada de endurecimento com
implementador e crítico dedicados por item. Ela encontrou dez defeitos graves, e
os dez eram da mesma família: **configurado e inerte.** O campo estava preenchido,
o dashboard mostrava o número, a suíte passava, e a proteção não agia.

Quatro regras saíram disso, e valem para todo teste novo:

1. **Medir, nunca supor.** Cinco desses defeitos pareciam corretos lendo o código.
2. **Um número numa janela não prova nada.** Três filtros de regime foram medidos
   e os três desligados por não replicarem fora da janela onde foram escolhidos.
3. **O estado seguro não é "arrisca menos", é "não arrisca".**
4. **Provar que a proteção AGE.** Teste que confere que o campo está preenchido
   não vale; o teste tem que ver a proteção disparar — e a contraprova é desligar
   a correção e ver o ataque voltar a funcionar.

Detalhe de cada defeito e da medição que o provou em
[`SEGURANCA.md` §16](docs/SEGURANCA.md).

## Aviso

Este projeto não é aconselhamento financeiro. Negociação de criptomoedas envolve
risco real de perda. Opere em `dry_run` até entender exatamente o que cada
estratégia faz, e comece com valores pequenos.
