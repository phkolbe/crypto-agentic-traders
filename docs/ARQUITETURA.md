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

## Ciclo de vida de um agente

`BaseAgent` cuida de start/pause/resume/stop/restart, heartbeat e tratamento de
erro. Três coisas ali não são gosto de arquitetura — são consequência de defeitos
medidos, e quem escrever um agente novo precisa conhecê-las.

**1. A batida de ocioso.** Um agente orientado a evento só batia heartbeat quando
recebia trabalho. Com candle diário ele fica legitimamente parado por horas, e o
watchdog o declarou travado e o reiniciou 17 vezes em 16 minutos. O sinal correto
de vitalidade é "estou vivo esperando", não "recebi trabalho". O timeout de 10
minutos **não** foi afrouxado — afrouxá-lo trocaria um alarme falso por um
travamento real não detectado, que é a troca errada.

**2. A vitalidade é por TAREFA, não por agente.** A primeira versão da batida de
ocioso guardava um único "estou esperando" por agente, e isso espelhou o defeito
em vez de corrigi-lo. O Risk Manager tem duas tarefas — uma que alimenta a fila
de sinais e outra que avalia os lotes —, e quem marcava a espera era a
alimentadora. Com o laço de avaliação pendurado para sempre, o alimentador seguia
ocioso, o agente seguia batendo e o watchdog via verde: **o guardião pelo qual
toda ordem passa podia travar indefinidamente.** Antes o ocioso parecia travado;
depois o travado parecia ocioso. Hoje cada tarefa vigiada reivindica a própria
espera, e o agente só bate como ocioso quando **todas** estão esperando.

> Por isso tarefa interna de agente se cria com `self.spawn()`, nunca com
> `asyncio.create_task`: o que não é vigiado não conta para a vitalidade, e uma
> tarefa fora do registro é exatamente o ponto cego acima. Existe teste que varre
> o pacote `agents` cobrando essa regra.

**3. A caixa de entrada é durável e pertence ao AGENTE, não à tarefa.** O reinício
era `stop()` + `start()`, e entre os dois o agente não estava assinando o tópico.
Candle é publicado uma única vez (fechado e inédito), e o Market Data publica os
16 pares em rajada com ~0,4 s entre eles — um reinício caindo na rajada derrubava
vários, sem uma linha no log dizendo "sinal perdido". Medido: pelo caminho antigo
o agente perdia **16 de 16** da rajada; pelo novo recebe 16.

A assinatura e a fila vivem fora do ciclo de vida da tarefa de processamento, e só
os tópicos de evento **único** ganham caixa durável:

| Tópicos | Caixa durável? | Por quê |
|---|---|---|
| `CANDLES`, `SIGNALS`, `ORDER_REQUESTS` | **sim** | publicados uma vez cada; perder um é perder trabalho para sempre |
| preço, snapshot, heartbeat, alerta | não | estado repetido — a próxima publicação substitui a anterior |

A caixa tem teto (1000, igual ao do bus). Fila sem teto guardando eventos para um
consumidor que nunca volta cresce sem limite; com teto, a pressão volta para o
bus, que já descarta **com aviso**. Perder com aviso é melhor que crescer em
silêncio.

## Pausar não é bloquear

Distinção estrutural, e a mais fácil de errar: **pausar o Execution Agent desliga
o stop-loss.** Por isso o orquestrador nunca o pausa. A trava desce como
*bloqueio de abertura* (`block_openings`), que recusa `Side.BUY` e deixa passar
`Side.SELL` — exatamente a assimetria de D4.

Isso vale para os três caminhos que "param" o sistema: o circuit breaker, a pausa
pela interface e a indisponibilidade da execução detectada pelo orquestrador. Os
três chamam o bloqueio de abertura; nenhum pausa quem fecha posição. Detalhe e
medições na seção 2 de [`SEGURANCA.md`](SEGURANCA.md).

Consequência para o dashboard: a execução sob trava aparece com
`openings_blocked` preenchido, não como `paused` — `paused` seria sempre `False`
e o operador não veria que a compra está barrada.

## Observabilidade: os estados que parecem saudáveis

`Orchestrator.health()` expõe, por agente, campos que existem porque cada um deles
já foi um estado indistinguível de "tudo certo":

| Campo | O estado que ele nomeia |
|---|---|
| `idle` / `idle_detail` | ocioso por direito — não é o mesmo que travado |
| `deaf_topics` | de pé, sem erro, sem eventos: **surdo**. O estado que mais parece saudável |
| `pending_events` / `lost_events` | fila crescendo, ou evento descartado |
| `inbox_failures` / `restarts` / `given_up` | falha na caixa, churn de reinício, desistência do watchdog |
| `openings_blocked` (execução) | compra barrada com o agente rodando |
| `tasks` | as tarefas do **orquestrador**, porque uma delas morta (alertas) apaga o único caminho de notificação |

A supervisão é mútua: o watchdog vigia os agentes, e o caminho movido pelo
Portfolio Agent (`_on_snapshot`) vigia as tarefas do próprio orquestrador — é dali
que a morte do watchdog é vista. A ordem dentro desse caminho é deliberada:
ressuscitar as tarefas próprias vem **antes** de publicar o aviso de posições sem
stop, porque um listener de alertas morto faria o aviso não chegar a ninguém.
Ressuscitar o carteiro antes de mandar a carta.

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

### Defesas dentro do banco

O append-only deixou de ser só contrato de repositório e passou a ter duas camadas
no banco:

- **Gatilhos.** `audit_log` recusa `UPDATE` e `DELETE`; `trades` recusa `UPDATE` e
  só aceita `DELETE` de lançamento manual. Existem para SQLite e para PostgreSQL,
  com um teste que confere a lista de gatilhos esperados em cada dialeto.
- **O autorizador do driver.** No SQLite, `sqlite3_set_authorizer` é consultado ao
  **preparar** cada comando, depois do parser, e recebe o que o comando faz — não
  o texto dele. Substituiu uma barreira de regex que caía com comentário no meio
  do comando e com pragma qualificado por schema.

A limitação honesta dessa camada está na seção 5 de
[`SEGURANCA.md`](SEGURANCA.md): contra código hostil **dentro deste processo** não
há barreira do lado do SQLite, porque `setconfig` não é SQL. Fechar exigiria
separação de privilégio, que só o PostgreSQL oferece.

## Como o patrimônio é apurado

O Portfolio Agent não produz número de relatório: o **preço médio** é a única
entrada do nível de stop, e o **resultado de negociação** é o número que arma ou
desarma o circuit breaker. Errar a conta aqui vende dinheiro real sem perda
nenhuma, ou deixa de proteger diante de prejuízo de verdade.

Uma varredura única do histórico de trades produz as duas coisas, e três decisões
governam essa varredura:

**1. O modo separa o dinheiro.** Valem os trades do modo **corrente**, mais os
`manual`. O ensaio em `dry_run` roda contra o mesmo banco que será usado em LIVE,
e sem o filtro uma compra de papel a 100 contamina o médio da compra real a 50. O
filtro não é "ignorar papel": excluir `dry_run` sempre deixaria o ensaio sem preço
médio, logo **sem stop**. Detalhe na seção 4 de [`SEGURANCA.md`](SEGURANCA.md).

**2. O realizado é derivado do histórico**, por custo médio móvel, e não somado da
coluna `trades.realized_pnl` — aquela coluna só é preenchida pelo Execution Agent
quando ele mesmo fecha a posição que abriu. Não há corte por âncora de tempo:
`executed_at` não é monotônico, e cortar por ele fazia um lançamento datado no
futuro somar o mesmo lucro 1.440 vezes por dia.

**3. Saldo e histórico são de instantes diferentes.** A exchange responde o saldo
de agora; a linha em `trades` chega depois. A quantidade que já saiu do saldo e
ainda consta no histórico é um **fantasma**, e é o razão de fantasmas que
reconcilia os dois — uma vez por fantasma, desfazendo o crédito quando ele deixa
de existir. O razão mora no `audit_log` porque precisa durar entre reinícios, ser
append-only e ser legível por uma pessoa; o registro do ajuste **é** o razão.

Tudo o que a apuração descarta — trade de outro modo, de outra cotação, taxa,
venda sem base de custo — vai para o `audit_log`. Por isso a consulta que alimenta
a apuração é deliberadamente **sem filtro**: quem apura precisa ver o que vai
descartar. Filtrar na consulta deixaria o descarte invisível.

### Preço por par, não por ativo

`latest_prices` era indexado pelo ativo base, e `BTC/USDC` e `BTC/USDT` gravavam
na **mesma** chave `"BTC"`: o último par da rajada vencia, em silêncio, e o preço
de um mercado avaliava a posição do outro. Não é cenário teórico — a lista de
pares é editável pela interface e o sistema acabou de migrar de BRL para USDC.

A regra hoje: o ativo base continua sendo a chave **apenas** do par cotado na
moeda de cotação do sistema; qualquer outro par guarda o preço sob o nome
completo. O motivo é de unidade, não de organização — Portfolio e Risk leem esse
dicionário por ativo, multiplicam pela quantidade em carteira e somam ao caixa,
que está em `quote_currency`. Somar um preço em outra unidade seria erro de conta,
não arredondamento. O par divergente não se perde (continua publicado, gravado e
visível no dashboard) e não pode ser confundido: quem lê por ativo simplesmente
não o encontra, e preço ausente já tem caminho conservador.

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
seção 14 de [`SEGURANCA.md`](SEGURANCA.md) para o episódio que tornou isso uma
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

O bundle hoje é **283 kB** (era 268,2 kB na saída do Recharts): o endurecimento
das telas acrescentou ~15 kB de lógica de rótulo, moeda e contraste. Nenhuma
biblioteca de gráfico foi reintroduzida.

### O que o `tsc` não vê, e o harness vê

`npx tsc -b` e `npx vite build` passam limpos com a tela errada na frente do
usuário — os defeitos encontrados no endurecimento tinham todos a mesma
assinatura: **tipo certo, tela errada.** Eixo X com oito rótulos idênticos, eixo Y
colapsando três marcações em "29", a tela anunciando USDT depois da migração para
USDC, um tooltip afirmando "150,00 USDC" sobre um valor de 150 BRL, texto
secundário abaixo do contraste mínimo de WCAG AA (3,77 no tema escuro, 3,04 no
claro), `.btn` usado nas telas e nunca declarado no CSS.

`src/checks/telas.check.ts`, rodado com `npm run check`, faz **174 verificações**
sem framework de teste novo (usa o esbuild que o Vite já traz). Duas regras que
ele segue, e que valem para qualquer verificação nova:

1. **Casar string de código-fonte não prova quase nada.** Um regex sobre
   `event.decision === null ?` passa com qualquer lógica atrás do ternário. As
   decisões de exibição vivem em funções puras em `format.ts` e o harness as
   **chama** com os dados reais do sistema.
2. **Toda alegação de conserto precisa de uma verificação que veja o DEFEITO
   também.** Cada bloco tem par: "o defeito existe" e "o conserto age".

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
documentadas na seção 12 de [`SEGURANCA.md`](SEGURANCA.md), **e em produção**, no
`RiskManagerAgent.enforce_protective_exits`, que compara cada posição contra os
níveis a cada snapshot.

O que continua não existindo é a proteção **do lado da exchange**:
`ccxt_adapter.place_order` não envia `stopPrice` nem OCO. A distinção não é
detalhe — é a diferença entre "protegido enquanto o processo vive" e "protegido".
A seção 12 do `SEGURANCA.md` lista, uma por uma, as oito situações em que uma
posição fica sem stop; leia antes de assumir cobertura.

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
| Postgres/TimescaleDB + Redis via Docker | SQLite + bus in-process, com adapters Postgres/Redis prontos | Docker não instalado na máquina; troca é uma linha no `.env`, sem retrabalho — mas **nunca exercitada**: os 6 testes de Postgres são `skipped` |
| Circuit breaker "pausa todos os agentes" | **bloqueia a abertura**; nunca pausa quem fecha posição | pausar o Execution Agent desligava o stop-loss justamente quando o mercado cai. Medido: execução ativa 1 ordem `sell`, pausada **0** |
| Stop-loss como parâmetro calculado | comparado contra preço a cada snapshot, com registro em `risk_events` antes da ordem | parâmetro inerte não falha: era calculado, gravado e exposto, e nunca comparado com preço nenhum |
| `pandas-ta` para indicadores | Indicadores próprios em pandas/numpy | `pandas-ta` não acompanha o pandas 3.0; e o Risk Manager exige cobertura de teste sobre cálculo próprio, não caixa-preta |
| `vectorbt`/`backtrader` para backtest | Backtester próprio orientado a eventos | Reutiliza o **mesmo** Strategy Agent, Risk Manager e PaperBroker da produção — testa o código real, não uma reimplementação |
| Celery/APScheduler | Loops `asyncio` no orquestrador | Um processo só; agendamento externo adicionaria infraestrutura sem ganho |
| Coinbase no MVP | Somente Binance | Coinbase movida para a fase 5; o adapter `ccxt` já é genérico |
| Whitelist de pares sempre fixa | Lista de pares vazia ativa descoberta automática | Universo escolhido por liquidez, com a lista descoberta virando a whitelist efetiva e indo para o `audit_log` |
| Alertas por Telegram | E-mail (SMTP) e WhatsApp (Meta Cloud API) | Canais que o operador de fato usa, cada um com liga/desliga na interface |
| Recharts para os gráficos | Gráficos próprios em SVG (`components/Charts.tsx`) | Recharts e o que ele arrasta (lodash, d3-*, react-smooth, decimal.js-light) eram **60% do bundle** para desenhar três gráficos: 691 kB -> 268 kB ao sair |
| Configuração toda no `.env` | `.env` só ambiente; negócio no banco, editável na web | Enquanto os limites viviam nos dois lugares, o banco vencia em silêncio — o `.env` pedia ordem máxima de 7 USDT e o sistema operava com 50 |
