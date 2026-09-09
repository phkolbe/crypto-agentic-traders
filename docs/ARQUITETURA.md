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

## Gráficos

Três gráficos — área do patrimônio, rosca de alocação, linha do backtest — em
`frontend/src/components/Charts.tsx`, ~380 linhas de SVG.

A medição que motivou sair do Recharts, atribuindo bytes por pacote via
sourcemap:

| | Antes | Depois |
|---|---|---|
| Bundle | 691,6 kB | **268,2 kB** |
| Comprimido (gzip) | 198,0 kB | **84,5 kB** |
| Módulos transformados | 706 | **89** |
| Tempo de build | 4,1 s | **0,74 s** |

Recharts pesava 618 kB de fonte e arrastava lodash (151 kB), decimal.js-light,
react-smooth, d3-scale/shape/time/format/color e recharts-scale — somando ~60%
do bundle para desenhar três gráficos.

O que foi preciso reimplementar, e o que cada peça resolve:

- **Interpolação monotônica** (tangentes de Fritsch–Carlson), que era o
  `type="monotone"`. Uma spline cúbica comum inventa oscilação entre pontos —
  numa curva de patrimônio isso desenharia prejuízo onde não houve.
- **Marcações "redondas"** no eixo Y, com o passo arredondado **para cima**:
  arredondar para baixo respeita o valor redondo e estoura a contagem, e num
  gráfico de 110 px os rótulos saíam empilhados.
- **Rarefação do eixo X pela largura do rótulo**, não por um espaçamento fixo:
  com 500 pontos e datas como "08 de set.", um `minTickGap` de 50 px deixava os
  rótulos a 50 px de distância e 55 px de largura.
- **Anel fechado para fatia única**, porque um arco de 360° tem começo e fim no
  mesmo ponto e não pinta nada. Esse era justamente o caso em que o donut do
  Recharts sumia, e agora não existe animação nem estado interno para congelar.

O maior item restante é o `react-router` (308 kB de fonte, 48% do que sobrou) —
mais que o `react-dom`. Trocá-lo por um roteador mínimo é viável, já que as 8
rotas são planas e sem parâmetros, mas mexe em histórico e deep link: categoria
de risco diferente de um renderizador de gráfico, cujo resultado se verifica
olhando.

## O backtest reusa o código de produção — e por que isso não bastou

O backtester instancia o **mesmo** Strategy Agent, o **mesmo** `RiskEngine` e o
**mesmo** `PaperBroker` da produção. A intenção é testar o código real, não uma
reimplementação que divirja com o tempo.

Isso pegou muita coisa. Não pegou o stop-loss, e a razão é instrutiva: reusar o
componente que **calcula** o nível não diz nada sobre quem o **executa**. O
`RiskEngine` fazia sua parte corretamente; o elo seguinte não existia, nem no
backtest nem na produção — e um elo ausente não quebra teste nenhum.

O padrão a lembrar: quando um parâmetro de configuração não muda nenhum
resultado medido, isso é evidência, não ruído. Foi assim que apareceu — 480 de
480 pares idênticos ao variar `stop_loss_pct`.

Hoje a execução da proteção existe nos dois motores de backtest
(`_apply_protective_exits`), com convenções deliberadamente pessimistas
documentadas na seção 11 de [`SEGURANCA.md`](SEGURANCA.md). Em produção **ainda
não existe**: `ccxt_adapter.place_order` não envia `stopPrice` nem OCO.

## Quem ganha o caixa quando os sinais competem

`RiskEngine.evaluate_batch` ordena fechamentos primeiro e depois aberturas por
confiança decrescente. Mas isso **só vale quando existe lote**, e a janela de
agrupamento (`signal_batch_window_seconds`) está em zero por padrão — medição em
`SEGURANCA.md` seção 9 não sustentou ligá-la.

Consequência: em produção cada sinal é avaliado sozinho, e com o caixa cabendo em
~7 posições quem ganha é quem chegou primeiro — a ordem da lista de pares. O
backtest, que agrupa naturalmente os sinais de um mesmo fechamento de candle,
ordena por confiança. **Os dois usam critérios diferentes.**

Medido, e imaterial na configuração vigente: cinco critérios de disputa
(confiança, ordem da lista, ordem inversa, força relativa crescente e
decrescente) produzem **queda máxima, operações/mês e taxa de acerto idênticas**.
O conjunto de operações é praticamente o mesmo, porque com 16 operações por mês a
disputa por caixa quase não acontece.

A ressalva é condicional e importa se a configuração mudar: com timeframe mais
rápido ou mais estratégias (as três juntas dão 33 operações/mês), a disputa passa
a ser frequente e alinhar os dois critérios deixa de ser cosmético.

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
| Recharts para os gráficos | Gráficos próprios em SVG (`components/Charts.tsx`) | Recharts e o que ele arrasta (lodash, d3-*, react-smooth, decimal.js-light) eram **60% do bundle** para desenhar três gráficos: 691 kB -> 268 kB ao sair |
| Configuração toda no `.env` | `.env` só ambiente; negócio no banco, editável na web | Enquanto os limites viviam nos dois lugares, o banco vencia em silêncio — o `.env` pedia ordem máxima de 7 USDT e o sistema operava com 50 |
