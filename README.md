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

Detalhamento em [`docs/ARQUITETURA.md`](docs/ARQUITETURA.md).

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

## Segurança

Resumo do que o sistema garante estruturalmente:

- Chaves de API **nunca** em código, banco ou arquivo versionado — só no `.env`
  (que está no `.gitignore`), carregadas como `SecretStr` e redigidas no log.
- Crie as chaves **sem permissão de saque** e com **whitelist de IP**.
- Toda ordem passa pelo Risk Manager: limite por operação, whitelist de ativos,
  exposição máxima por ativo, cooldown, stop-loss/take-profit obrigatórios.
- Circuit breaker de perda diária/semanal pausa tudo e **só rearma manualmente**.
- Idempotência por `client_order_id`: um retry de rede não duplica ordem.
- `audit_log` append-only para toda mudança de configuração e ação administrativa.
- API escuta apenas em `127.0.0.1`.

Detalhamento em [`docs/SEGURANCA.md`](docs/SEGURANCA.md).

## Testes

```bash
uv run pytest
```

O Risk Manager é o componente com cobertura mais alta — é ele que separa
"autônomo" de "descontrolado".

## Aviso

Este projeto não é aconselhamento financeiro. Negociação de criptomoedas envolve
risco real de perda. Opere em `dry_run` até entender exatamente o que cada
estratégia faz, e comece com valores pequenos.
