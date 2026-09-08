# Arquitetura

## Princípio central

O sistema é dividido em agentes com **uma responsabilidade cada**, ligados por um
event bus. A divisão não é estética: ela existe para que a decisão de operar
possa ser auditada, limitada e testada etapa por etapa.

A regra estrutural mais importante:

> O Strategy Agent **nunca** executa ordens. O Execution Agent **nunca** decide.
> Entre os dois há sempre o Risk Manager.

Isso não é uma convenção de código — é imposto pelos tipos. O Execution Agent só
aceita um `OrderRequest`, e `OrderRequest` carrega um campo obrigatório
`risk_event_id`, que só o Risk Manager sabe preencher (ele é a chave da linha
gravada em `risk_events`). Um sinal não consegue virar ordem por atalho.

## Fluxo de uma decisão

```
Market Data Agent
   │  Candle (fechado)
   ▼
Strategy Agent ──── calcula indicadores, aplica estratégias
   │  Signal (direção, confiança, motivo, indicadores)
   ▼
Risk Manager Agent ── aplica TODAS as regras de risco
   │                   grava RiskAssessment (aprovado OU rejeitado)
   │  OrderRequest (com stop-loss e take-profit anexados)
   ▼
Execution Agent ──── grava PENDING, envia, reconcilia
   │  OrderResult
   ▼
Portfolio Agent ──── saldos, PnL, snapshot da série temporal
   │
   ▼
API (FastAPI) ──► WebSocket ──► Dashboard React
```

## Por que candle fechado

Candles em formação têm `closed=False` e são descartados pelo Strategy Agent. Um
sinal calculado sobre um candle ainda em movimento pode aparecer e desaparecer
dentro do mesmo minuto — e cada oscilação dessas viraria uma ordem.

## Agentes

| Agente | Responsabilidade | Toca dinheiro? |
|---|---|---|
| Market Data | Busca candles/ticker, normaliza, persiste, publica | Não — usa só endpoints públicos, sem credenciais |
| Strategy | Calcula indicadores, roda estratégias, emite `Signal` | Não |
| Risk Manager | Valida limites, anexa SL/TP, circuit breaker, audita | Não — mas é quem autoriza |
| Execution | Envia ordem, reconcilia status, garante idempotência | **Sim** — único componente que pode |
| Portfolio | Saldos, posições, PnL, snapshots | Não — só leitura |
| Orquestrador | Agenda ciclos, heartbeats, pausa/retoma, restart | Não |

O Market Data Agent recebe uma `MarketDataSource` construída **sem credenciais**
(`build_market_data_source`), enquanto o Execution Agent recebe um `Broker`. São
interfaces separadas de propósito: o código com poder de gastar fica em uma
superfície pequena e revisável.

## Event bus

Contrato único (`EventBus`), dois backends:

- **`memory`** (padrão) — filas `asyncio`, uma por assinante, fan-out real.
  Sem infraestrutura, roda em qualquer máquina.
- **`redis`** — Redis Streams. Mesma semântica, e ainda permite *replay*: os
  eventos ficam no stream e podem ser reprocessados para auditoria.

Trocar é uma linha no `.env`. Nenhum agente sabe qual backend está em uso.

Quando um consumidor lento enche sua fila, o bus in-process descarta o evento
**mais antigo** e registra `event_bus.queue_full`. Bloquear o produtor seria pior:
travaria o Market Data Agent e, com ele, todo o resto.

## Persistência

Mesmo schema para SQLite e PostgreSQL/TimescaleDB — os tipos usados existem nos
dois. Valores monetários usam o tipo `Money`: texto no SQLite (decimal exato) e
`Numeric(38,18)` no PostgreSQL. Nunca `Float`.

No Postgres, `candles` e `portfolio_snapshots` viram *hypertables* do TimescaleDB
automaticamente no `init_db()`. Se a extensão não existir, o sistema registra um
aviso e segue com tabelas comuns — Timescale é otimização, não requisito.

No SQLite ligamos WAL: sem ele, a escrita dos agentes bloqueia a leitura da API e
o dashboard congela justamente quando há atividade.

### Tabelas

| Tabela | Papel |
|---|---|
| `candles` | Série temporal OHLCV (backtest + indicadores) |
| `signals` | Todo sinal gerado, com os indicadores que o motivaram |
| `risk_events` | Toda decisão do Risk Manager — **inclusive rejeições** |
| `orders` | Ciclo de vida de cada ordem, com `client_order_id` único |
| `trades` | Histórico consolidado: agentes **e** lançamentos manuais |
| `portfolio_snapshots` | Série temporal do patrimônio (gráfico do dashboard) |
| `agent_runs` | Heartbeats |
| `audit_log` | Append-only: mudanças de config e ações administrativas |
| `onchain_metrics` | Série diária do MVRV Z-Score, cacheada (provedor externo) |
| `trading_config` | O que negociar e com que cadência (negócio, nunca do `.env`) |
| `risk_config` | Limites vigentes + estado do circuit breaker |
| `notification_config` | Canais de alerta: liga/desliga e destinatários (nunca segredos) |

`trades` é uma tabela só para agentes e lançamentos manuais, distinguidos por
`origin`. Isso mantém filtros, dashboard e exportação fiscal simples — sem UNION.

## Configuração: duas naturezas, dois lugares

```
.env  (ambiente)                      banco  (negócio)
├── credenciais                       ├── trading_config
├── TRADING_MODE, LIVE_CONFIRMED      │   pares, moeda, timeframe,
├── EXCHANGE                          │   estratégias, cadência,
├── DATABASE_URL, EVENT_BUS           │   descoberta, simulação
├── API_HOST/PORT, CORS               └── risk_config
└── SMTP_*, WHATSAPP_*                    todos os limites + MVRV
        │                                        │
        ▼                                        ▼
   Settings (BaseSettings)          TradingSettings / RiskSettings
   lê ambiente                      BaseModel puros: NÃO leem ambiente
        └──────────── install_business_config() ──────┘
                    (no start, antes dos agentes)
```

`Settings.trading` e `Settings.risk` existem por conveniência de leitura —
`settings.trading.timeframe` — mas quem os preenche é o banco, na subida. A troca
é do objeto inteiro (`with_business_config`), não campo a campo: a leitura fica
atômica e nenhum agente vê metade da configuração nova. Como todos leem
`settings.trading.X` no momento do uso, a troca se propaga sozinha, sem cópias
envelhecendo em cada agente.

Três coisas não se propagam sozinhas, porque são lidas na **construção** do
agente, e `Orchestrator.update_trading` reage a elas: a lista de estratégias
(instanciada uma vez), os critérios de descoberta (que só revarrem a cada 24h) e
a mudança de universo. Sem isso a tela mostraria uma configuração que o sistema
não está usando.

Escrever uma variável de negócio no `.env` faz `Settings` recusar subir. Ver
seção 11 de [`SEGURANCA.md`](SEGURANCA.md) para o episódio que tornou isso uma
regra.

## Decisões que divergem do plano original

| Plano | Implementado | Motivo |
|---|---|---|
| Postgres/TimescaleDB + Redis via Docker | SQLite + bus in-process, com adapters Postgres/Redis prontos | Docker não instalado na máquina; troca é uma linha no `.env`, sem retrabalho |
| `pandas-ta` para indicadores | Indicadores próprios em pandas/numpy | `pandas-ta` não acompanha o pandas 3.0; e o Risk Manager exige cobertura de teste sobre cálculo próprio, não caixa-preta |
| `vectorbt`/`backtrader` para backtest | Backtester próprio orientado a eventos | Reutiliza o **mesmo** Strategy Agent, Risk Manager e PaperBroker da produção — testa o código real, não uma reimplementação |
| Celery/APScheduler | Loops `asyncio` no orquestrador | Um processo só; agendamento externo adicionaria infraestrutura sem ganho |
| Coinbase no MVP | Somente Binance | Coinbase movida para a fase 5; o adapter `ccxt` já é genérico |
| Whitelist de pares sempre fixa | Lista de pares vazia ativa descoberta automática | Universo escolhido por liquidez, com a lista descoberta virando a whitelist efetiva e indo para o `audit_log` |
| Alertas por Telegram | E-mail (SMTP) e WhatsApp (Meta Cloud API) | Canais que o operador de fato usa, cada um com liga/desliga na interface |
| Configuração toda no `.env` | `.env` só ambiente; negócio no banco, editável na web | Enquanto os limites viviam nos dois lugares, o banco vencia em silêncio — o `.env` pedia ordem máxima de 7 USDT e o sistema operava com 50 |
