# Segurança

Segurança aqui é tratada em camadas independentes. A premissa é que **qualquer
uma delas pode falhar** — por bug, descuido de configuração ou comportamento
inesperado do mercado — e as demais precisam segurar o prejuízo.

---

## 1. Credenciais

### Ao criar as chaves na exchange

1. **Permissão de leitura + negociação apenas. NUNCA habilite saque/withdraw.**
   Esta é a única camada que a aplicação não consegue impor sozinha — e é a que
   limita o pior cenário possível de "perda total" para "perda do que estava na
   conta de trading".
2. **Whitelist de IP**, restrita ao IP desta máquina.
3. **Rotação periódica**, e imediata a qualquer suspeita de exposição.

### Whitelist de IP: o detalhe que mais morde

Restringir a chave a um IP é a segunda camada mais valiosa depois de negar
saque. Mas num IP **residencial e dinâmico** — o caso típico no Brasil — ele
muda sozinho: reinício do modem, renovação da concessão do provedor, ou nada
aparente.

Quando isso acontece, a falha é silenciosa e assimétrica:

- Os dados de mercado são **públicos** e continuam chegando. O dashboard segue
  atualizando preços, os agentes continuam com heartbeat verde, e nada na tela
  indica problema.
- Nenhuma ordem consegue mais sair. Se houver **posição aberta**, o sinal de
  fechamento é aprovado pelo Risk Manager e a ordem morre na exchange —
  **stop-loss e take-profit deixam de existir na prática.**

Ou seja: a proteção contra chave vazada cria uma janela em que a proteção contra
prejuízo não funciona. Por isso o sistema tem duas defesas específicas:

1. **`crypto-traders check` valida contra um endpoint autenticado.** A checagem
   de conectividade usa endpoints públicos, que funcionam mesmo com a chave
   bloqueada; sem a sonda autenticada o diagnóstico diria "tudo pronto" para um
   sistema incapaz de enviar uma ordem. Quando o acesso é negado, o comando
   imprime o IP de saída atual da máquina, que é o que resolve o caso na maioria
   das vezes.

2. **O Execution Agent reconhece esse erro especificamente** (`ApiAccessDenied`)
   e dispara alerta imediato dizendo que há risco de posição sem stop — em vez
   de registrar "ordem falhou" junto com um timeout de rede qualquer. O alerta
   sai **uma vez por incidente**, não por ordem: com o IP fora da whitelist toda
   ordem falha, e spam faz o operador ignorar justamente o aviso que importa. A
   recuperação também é anunciada.

Note que `-2015` na Binance ("Invalid API-key, IP, or permissions") é ambíguo por
natureza — cobre chave, IP e permissão na mesma mensagem. O sistema aponta o IP
como causa provável nesse código, mas não em `-2014` (formato da chave) nem em
`-1022` (assinatura), que são problemas da credencial e mandariam investigar o
lugar errado.

**A solução real é um IP de saída estável**: VPN pessoal com *exit node*
(Tailscale), IP fixo contratado, ou rodar numa VPS. Whitelist do bloco inteiro do
provedor não serve — autoriza milhares de clientes e esvazia a proteção.

### No sistema

- Chaves vivem apenas no `.env`, que está no `.gitignore`.
- São carregadas como `SecretStr` do Pydantic: o valor não aparece em `repr()`,
  em tracebacks nem em dumps de configuração.
- O logger tem um processador (`_redact_secrets`) que substitui por `***`
  qualquer campo cujo nome sugira credencial. É uma última linha de defesa:
  se um agente logar um dict de credenciais por descuido, nada vaza para o disco.
- Nenhuma chave é gravada no banco.

> **Nunca cole suas chaves em um chat, e-mail ou issue** — nem para configurar
> este sistema. Elas devem ser digitadas apenas no `.env` local.

---

### Modo "mar aberto": o que ele custa em segurança

Com `SYMBOLS` em branco o sistema **descobre os pares sozinho**, varrendo a
exchange e escolhendo os mais líquidos. É conveniente, e tem um preço que
precisa estar claro.

A **whitelist de ativos** era uma das camadas de proteção do plano original: uma
lista curta, aprovada por uma pessoa, que impedia o sistema de tocar em token
ilíquido ou desconhecido. No modo automático ela deixa de ser uma lista aprovada
a mão e passa a ser um **conjunto de critérios**. Isso é uma proteção mais fraca.

As compensações, todas objetivas:

| Filtro | Por quê |
|---|---|
| Volume mínimo em 24h | Dos 487 pares USDT da Binance, **312 movimentam menos de 1M/dia**. Neles a própria ordem move o preço. |
| Teto de pares | Sem teto, "mar aberto" vira dezenas de posições simultâneas. |
| Exclusão de stablecoins | USDC/USDT é o **maior volume** da Binance e o pior par possível aqui: o preço não anda, então todo sinal é ruído e toda operação é taxa. |
| Só mercados spot e ativos | Evita par deslistado ou de outro tipo. |

E o mais importante: **a lista descoberta vira a whitelist efetiva** e vai para
o `audit_log` a cada mudança. Em qualquer instante existe uma lista concreta e
inspecionável do que o sistema pode negociar — ela apenas deixou de ser digitada
a mão.

Com isso, as defesas que passam a carregar o peso são `RISK_MAX_OPEN_POSITIONS`,
`RISK_MAX_ASSET_EXPOSURE_PCT` e o tamanho máximo por ordem. **Revise esses três
antes de usar o modo automático.**

Não há filtro de "token alavancado" por sufixo, de propósito: procurar
`UP`/`DOWN`/`BULL`/`BEAR` no nome marca JUP (Jupiter), SYRUP e SUPER como
alavancados. Para excluir um ativo específico use **Ativos excluídos** em Configurações,
que é explícito e não erra.

---

## 2. Controle operacional (o Risk Manager)

Toda ordem passa por ele. As regras são cumulativas — basta uma violação para
rejeitar:

| Regra | Protege contra |
|---|---|
| Notional máximo por ordem (absoluto **e** % do portfólio) | Um bug de cálculo apostar o portfólio inteiro em uma operação |
| Notional mínimo | Ordens tão pequenas que a taxa consome o resultado |
| Whitelist de símbolo e de ativo | Operar um token ilíquido ou desconhecido |
| Exposição máxima por ativo | Concentração acidental depois de vários sinais na mesma direção |
| Máximo de posições abertas | Pulverização e perda de controle |
| Confiança mínima do sinal | Ruído da estratégia virando ordem |
| Cooldown por par | *Overtrading* — o mesmo sinal disparando repetidamente |
| Circuit breaker diário/semanal | Um dia ruim virar um mês ruim |

**Stop-loss e take-profit são calculados pelo Risk Manager**, não pela
estratégia. Uma estratégia nova, escrita meses depois, não tem como esquecer de
definir stop: ela nem participa dessa etapa.

> ⚠️ **Executados pelo sistema, não pela exchange.** O Risk Manager compara os
> níveis a cada snapshot e fecha a posição — ver seção 12 para o que essa escolha
> não cobre. Até a descoberta do defeito, este documento afirmava que os níveis
> eram "anexados a toda posição": eram calculados e nunca comparados com preço
> nenhum. É o tipo de erro que um leitor não teria como pegar, porque o número
> aparecia no dashboard.

### Patrimônio pequeno demais para os limites

Uma combinação silenciosa e enganosa: carteira pequena com limites conservadores
faz o Risk Manager rejeitar **todo** sinal, e o sistema fica de pé com heartbeat
verde, dashboard atualizando e nenhuma operação — parecendo saudável.

Números reais: R$100 (~19 USDT) com os padrões de fábrica dá 2% = **39 centavos**
por ordem, abaixo do mínimo de 10 USDT e do próprio mínimo da Binance (5 USDT).
Nenhuma ordem sairia jamais. O menor patrimônio que funciona sem mexer em limite
é ~500 USDT (R$2.600).

Por isso `assess_sizing_feasibility` avalia o **melhor cenário possível** (carteira
toda em caixa, sem posição no ativo). Se nem assim uma ordem passa, nenhuma
passará, e isso aparece em três lugares: o `check` falha com código 1, o
orquestrador dispara alerta no primeiro snapshot, e o dashboard mostra uma faixa
vermelha no topo.

O alerta sai **uma vez por transição**, não a cada snapshot — o Portfolio Agent
produz um por minuto, e avisar sempre viraria ruído.

Detalhe que importa no diagnóstico: o gargalo nem sempre é o percentual por
ordem. Em R$100, mesmo subindo o **percentual máximo por ordem** para 55%, quem
travava era a **exposição máxima por ativo** (30%). A mensagem nomeia o limite
que de fato está travando, porque apontar o errado manda investigar em vão.

### Circuit breaker

Se o portfólio cair além do limite configurado (diário ou semanal), todos os
agentes são pausados e um alerta é disparado. **O rearme é sempre manual**, nunca
automático por tempo: se o sistema perdeu dinheiro rápido o suficiente para
disparar a trava, a causa precisa ser entendida por uma pessoa antes de voltar.

---

## 3. Execução

- **Idempotência**: cada ordem tem um `client_order_id` único, com constraint
  `UNIQUE` no banco e enviado à exchange. Um retry de rede não vira ordem dupla.
- **Grava antes de enviar**: a ordem nasce `PENDING` no banco *antes* da chamada
  à exchange. Se o processo morrer no meio, resta o rastro para reconciliar — em
  vez de uma ordem existindo na exchange e em lugar nenhum aqui.
- **Retry só no que faz sentido**: timeout e rate limit são retentados com
  backoff exponencial. Saldo insuficiente e ordem inválida sobem na hora —
  repetir não muda o resultado e só atrasa o diagnóstico.

---

## 4. Modos de operação

O padrão de fábrica é `dry_run`: **nenhuma ordem sai da máquina**. Esquecer de
configurar algo resulta em simulação, nunca numa ordem real inesperada.

Ir para dinheiro real exige **duas** variáveis:

```dotenv
TRADING_MODE=live
LIVE_TRADING_CONFIRMED=true
```

Se só a primeira estiver ligada, a aplicação **se recusa a subir** com um erro
explicativo. Um caractere trocado não basta para começar a gastar.

O modo em vigor é gravado **em cada ordem e em cada trade** (coluna `mode`), então
o histórico nunca fica ambíguo sobre o que foi simulado e o que foi real.

---

## 5. Auditoria

- `risk_events` guarda **toda** decisão do Risk Manager, com o motivo de cada
  rejeição. É a resposta a "por que o agente não operou?" — pergunta tão
  importante quanto a inversa.
- `signals` guarda os valores dos indicadores no instante da decisão, então
  qualquer trade pode ser explicado depois sem recalcular nada.
- `audit_log` é **append-only por contrato**: o repositório não expõe `update`
  nem `delete`. Mudança de limite de risco, pausa de agente e ativação de LIVE
  passam obrigatoriamente por lá.
- Log estruturado em JSON (`LOG_JSON=true`) para indexação e busca.

---

## 6. Canais de alerta

Dois canais, cada um com liga/desliga próprio na tela **Notificações**: e-mail
(SMTP) e WhatsApp (Meta Cloud API).

A divisão entre os dois lugares de configuração é deliberada:

- **Segredos ficam só no `.env`** — senha de SMTP e token da Meta. Credencial em
  banco contraria a premissa do projeto: um backup do banco é um arquivo que
  circula, e se ele carregasse credenciais, um histórico de negociações vazado
  viraria também um acesso vazado ao seu e-mail e ao seu WhatsApp.
- **Preferências ficam no banco** e são editáveis pela interface — ligar cada
  canal e para onde enviar. A API nunca devolve segredo: informa apenas se está
  presente e, quando não, quais variáveis faltam.

Ligar um canal sem destinatário é recusado. Um canal "ligado" que nunca entrega
é pior que um desligado, porque cria a impressão de cobertura.

**Sobre o WhatsApp:** alertas são mensagens proativas e raras, então nunca existe
uma sessão de 24h aberta — e sem sessão o WhatsApp só aceita **template
aprovado**, nunca texto livre. É preciso criar no Meta for Developers um template
de categoria "utility" com dois parâmetros no corpo. A aplicação limpa quebras de
linha e tabs antes de enviar, porque a Meta rejeita parâmetros com eles — e um
alerta bem escrito tem exatamente isso.

Use o botão **Enviar teste** depois de configurar. O objetivo é não descobrir que
a notificação não funciona justamente no dia em que o circuit breaker dispara.

---

## 7. Infraestrutura local

- A API escuta **apenas em `127.0.0.1`**. O default nunca é `0.0.0.0`.
- Os serviços do `docker-compose.yml` publicam portas com prefixo `127.0.0.1:`,
  então nem o Postgres nem o Redis ficam acessíveis na rede local.
- CORS restrito à origem do dashboard.
- Para acessar o dashboard remotamente, use **VPN pessoal (Tailscale)** — nunca
  abra portas no roteador.
- Faça backup do banco: o histórico de negociações é dado sensível e é o que
  sustenta a apuração fiscal.

---

## 8. Rodar LIVE com carteira pequena

É possível, mas exige entender dois mínimos que se somam.

**O seu**, `RISK_MIN_ORDER_NOTIONAL`, e **o da exchange**, que na Binance são
dois: um valor mínimo por ordem (5 USDT na maioria dos pares spot) e um passo de
lote (`stepSize`) para o qual a quantidade é **truncada** antes do envio.

A armadilha está na ordem em que isso acontece. O ccxt trunca a quantidade, e só
então a Binance aplica o valor mínimo sobre o resultado. Em BTC/USDT o passo é
0,00001 e, a 79 mil, cada passo vale ~0,79 USDT — uma ordem mirando 5,50 vira
0,00006 BTC = **4,74 USDT** e é recusada. O mesmo valor passa tranquilo em ETH,
SOL ou DOGE, cujos passos são finos em relação ao preço.

Por isso o `check` simula a ordem contra os filtros reais de cada par:

```
Filtros da exchange (ordem de 5.50 USDT):
  NAO BTC/USDT          5.50 ->    4.75 USDT   (minimo da exchange: 5.0)
  OK  ETH/USDT          5.50 ->    5.47 USDT   (minimo da exchange: 5.0)
```

E falha com código 1, sugerindo o valor que funcionaria — com uma folga de um
passo, porque o preço se move entre a decisão e o envio, e uma ordem exatamente
na fronteira é rejeitada por qualquer variação contrária.

### Configuração validada para ~R$100 (19 USDT)

Na tela **Risco** (não no `.env` — ver seção 12):

| Limite | Valor |
|---|---|
| Valor máximo por ordem | 7 USDT |
| Máximo por ordem (% do portfólio) | 0,35 |
| Exposição máxima por ativo | 0,40 |
| Máximo de posições abertas | 2 |
| Valor mínimo por ordem | 6 USDT |
| Circuit breaker diário | 0,08 |

Ordem resultante: 6,75 USDT, que sobrevive ao arredondamento em BTC (6,33), ETH
(6,71) e SOL (6,73). O limite diário sobe para 8% porque, com ~70% da carteira
aplicada, uma queda normal do mercado dispararia o circuit breaker quase todo dia
em 5%.

### O que essa configuração custa

Não é pouco, e precisa estar explícito:

- **Concentração.** 35% da carteira num único ativo por operação, no máximo 2
  posições. A diversificação deixa de existir.
- **As taxas passam a pesar.** 0,1% por lado. Numa ordem de 6,75 são ~0,0135 USDT
  por ida e volta; a ~40 operações no mês, isso é **~2,8% do patrimônio só em
  taxa**, antes de qualquer resultado.
- **O resultado não significa nada estatisticamente.** Poucas operações medem a
  direção do mercado e sorte, não a qualidade da estratégia. O risco real não é
  perder o valor — é ter um ganho por acaso e concluir que o sistema funciona.

O que R$100 em live realmente valida é a **integração**: chave, IP, filtros da
exchange e preenchimento real. O **testnet** entrega exatamente isso de graça —
se o objetivo é validar o encanamento, use testnet primeiro.

---

## 9. Seleção de sinais: mecanismo pronto, desligado por evidência

Com muitos pares monitorados, sinais concorrem pelas mesmas vagas de posição.
Atender por ordem de chegada é arbitrário — num teste de 16 pares em 15m, **369
sinais foram rejeitados por "posições abertas (3/3)"**, descartados sem qualquer
comparação de qualidade.

`RiskEngine.evaluate_batch` resolve isso: junta os sinais concorrentes, avalia
**fechamentos primeiro** (fechar libera caixa e vaga, e travar a saída é a
armadilha que o sistema evita em todas as camadas) e depois as **aberturas por
confiança decrescente**, simulando cada aprovação para que a próxima veja o
caixa já consumido.

**E a medição não sustentou o ganho.** Ligado, o resultado piorou em 8 de 10
combinações testadas (4h, 5 e 16 pares BRL). A causa aparece ao correlacionar a
confiança de entrada com o PnL realizado:

| Estratégia | Correlação | Operações |
|---|---|---|
| ma_crossover | −0,09 | 92 |
| rsi_reversion | **−0,65** | 15 |
| macd_trend | +0,06 | 121 |
| todas as 4 | +0,05 | 198 |

A `confidence` das estratégias é heurística inventada — separação das médias,
profundidade do RSI, momento do MACD — e nunca foi validada como preditiva.
Ordenar por ela ordena por ruído.

O `rsi_reversion` é o caso instrutivo: a confiança cresce com a profundidade da
sobrevenda, mas cair mais fundo indica tendência de baixa mais forte.
Economicamente o sinal do coeficiente está invertido. Com 15 operações a
amostra é pequena para afirmar, mas é forte o suficiente para não usar.

Por isso `SIGNAL_BATCH_WINDOW_SECONDS=0` é o padrão: **o mecanismo existe, a
métrica que ele ordena não.** Ligá-lo faz sentido depois de construir uma medida
de qualidade validada contra resultado — não antes.

---

## 10. Filtro de regime por MVRV Z-Score

> **Todos os números desta seção foram refeitos** depois da descoberta de que o
> backtest não executava stop-loss. Ver seção 12.

**MVRV** = Market Value to Realized Value. O Z-Score normaliza a diferença entre
a capitalização e o preço médio que o mercado pagou, pelo desvio da
capitalização. Alto = lucro não realizado grande (historicamente perto de topo);
negativo = mercado agregado no prejuízo (historicamente fundo).

Três diferenças em relação aos indicadores de preço, e todas mudam o uso:

1. **É do Bitcoin.** Não existe MVRV por par. Serve como leitura do regime do
   mercado inteiro, apostando na correlação do resto com o BTC.
2. **É lento.** Move-se em meses. Em 4h é praticamente constante, então não gera
   sinal de entrada — serve de filtro.
3. **Não vem da exchange.** Depende de provedor externo
   (`bitcoin-data.com`), cacheado em `onchain_metrics`.

### Por que percentil e não o limiar clássico

A literatura cita `z > 7` como topo. Nos 4 anos de série disponíveis o **máximo
foi 3,35**, e não houve um único dia acima de 4. Um filtro no limiar clássico
ficaria inerte para sempre. O sistema usa **percentil da história observada**.

O custo dessa escolha: percentil é relativo à amostra. Se a série cobrisse só um
mercado de baixa, "caro" ali poderia ser barato em termos absolutos.

### O que a medição mostra, com stop funcionando

1d, 16 pares BRL, 998 dias, stop 3% / alvo 6%. Comprar e segurar: **−37,37%**.

| Estratégia | MVRV desligado | MVRV ≤ 40% | Queda desligado | Queda ≤ 40% |
|---|---|---|---|---|
| rsi_reversion | −16,39% | −5,40% | 20,5% | 10,1% |
| ma_crossover | **+6,91%** | +5,15% | 7,7% | **4,7%** |
| macd_trend | −6,07% | −0,24% | 11,2% | 2,4% |
| as três juntas | −16,66% | −4,62% | 23,8% | 11,2% |

Olhando só esta tabela, o filtro melhora quase tudo. **Mas a tabela engana**, e o
teste por janelas mostra por quê:

| Estratégia | MVRV | Janela 1 (2023-12→2024-11) | Janela 2 | Janela 3 |
|---|---|---|---|---|
| ma_crossover | desligado | +4,39% | +2,03% | +4,80% |
| ma_crossover | ≤ 40% | **0,00%** | **0,00%** | +4,80% |
| as três | desligado | −2,60% | −1,04% | −1,17% |
| as três | ≤ 40% | **0,00%** | **0,00%** | −1,94% |

**Zero por cento com zero de queda significa que o filtro bloqueou TODAS as
entradas.** Em dois terços do período o sistema não operou nenhuma vez. O
percentil é calculado só com o passado, e em 2024 o MVRV subia — ficava quase
sempre acima do percentil 40.

Ou seja: o ganho aparente do filtro na janela inteira é, em boa parte, **o ganho
de não ter operado**. Isso não é uma estratégia melhor; é abstenção. E na janela 1
custou caro: o mercado deu +24,2% e o sistema ficou de fora inteiro.

Em 4h (166 dias) o filtro quase não age e nada fica positivo, exceto
`macd_trend` com +1,67% — contra +13,3% do comprar e segurar.

`RISK_MVRV_MAX_PERCENTILE=1.0` (desligado) é o padrão. Ligá-lo em 0,40 nesta
configuração equivale, na prática, a **desligar o sistema** na maior parte do
tempo. Se é isso que se quer, desligar é mais honesto e mais barato.

Nunca bloqueia fechamento: o indicador diz "está caro", não "fique preso".
E dado ausente não bloqueia nada — sem o provedor, o sistema volta ao
comportamento sem filtro, que é o estado conhecido e testado.

### A série não pode congelar

O dado é buscado no start e atualizado a cada 12h por uma tarefa própria do
orquestrador. Sem essa tarefa a série parava no dia da subida e o filtro seguiria
respondendo com o percentil daquele dia por semanas. Dado velho que parece atual é
pior que dado nenhum: o `None` ao menos desliga o filtro de forma visível no log.

---

## 11. Três filtros de regime medidos, três desligados

O projeto testou três formas de "não operar quando o mercado está errado". Nenhuma
sobreviveu ao teste fora da janela. O padrão vale mais que os três resultados
somados, e está aqui para não ser refeito.

| Filtro | O que prometia | O que a medição deu |
|---|---|---|
| Seleção por confiança | escolher o melhor sinal da rajada | pior em 8 de 10 janelas |
| MVRV Z-Score | não comprar mercado caro | bloqueou **tudo** em 2 de 3 janelas |
| Fear & Greed | não comprar em euforia (ou em pânico) | ver abaixo |

### Fear & Greed: por que não entrou

O índice (alternative.me, o mesmo que a Binance exibe) tem 3.139 dias de
histórico, faixa completa de 5 a 95, e move 4,2 pontos por dia — 3,4× mais rápido
que o MVRV em fração da faixa. Isso o tornava promissor: ao contrário do MVRV, ele
não ficaria inerte por anos, e a escala 0–100 é absoluta e interpretável.

Testado nas **duas** direções sobre `ma_crossover`, 1d, 998 dias (base sem filtro:
+9,84%):

| Limiar | Não compre na ganância | Não compre no medo |
|---|---|---|
| percentil 90 | +9,89% | +6,80% |
| **percentil 80** | **+13,40%** | **+15,45%** |
| percentil 70 | +2,69% | +11,81% |
| percentil 60 | +8,91% | +5,03% |
| percentil 50 | +1,64% | +7,61% |
| percentil 40 | +3,99% | +0,32% |
| percentil 30 | −1,74% | +6,53% |

**Duas coisas condenam o resultado, e nenhuma delas é o valor de um número.**

**Primeiro: as duas regras opostas "funcionam" no mesmo limiar.** "Não compre
quando há ganância" dá +13,40% e "não compre quando há medo" dá +15,45%, ambas no
percentil 80. Regras contraditórias não podem estar capturando o mesmo efeito
real. O que variou foi *quais* operações específicas saíram — por sorte, não por
regime.

**Segundo: a resposta não é monótona.** Apertando o filtro: +9,89%, +13,40%,
+2,69%, +8,91%, +1,64%, +3,99%, −1,74%. Um efeito de regime seria aproximadamente
monótono — protegeria progressivamente mais, custando progressivamente mais
retorno. Este pula 12 pontos percentuais sem padrão.

E o teste fora da janela fecha:

| Configuração | Janela 1 | Janela 2 | Janela 3 | Soma |
|---|---|---|---|---|
| Sem filtro | +3,72% | +1,34% | +9,30% | 14,36% |
| Não compre na ganância (>73) | +1,43% | +1,83% | +10,34% | 13,60% |
| Não compre no medo (<30) | **+3,72%** | **+1,34%** | +12,65% | 17,70% |

"Não compre no medo" é **idêntico à base nas janelas 1 e 2** — o filtro nunca
disparou. Todo o ganho aparente vem da janela 3. É o mesmo desenho do MVRV: parece
melhorar no agregado porque agiu uma vez, e não agiu nas outras duas.

Uma ressalva honesta: na janela 3 (mercado caindo 20,8%) esse filtro cortou a
queda de 6,0% para 3,0% e metade das operações. Pode ser proteção real em mercado
de baixa — mas com uma única janela de baixa não há como distinguir isso de ter
bloqueado as operações certas por acaso. Testar essa hipótese exigiria mais
períodos de baixa do que a série disponível oferece.

### O que o padrão sugere

Três indicadores, três direções, sempre o mesmo desfecho: o filtro melhora a
janela onde foi escolhido e não replica. O que **de fato** reduziu queda de forma
consistente nas três janelas foi o stop-loss — que não é filtro de regime, é
limite por operação. Vale registrar para a próxima vez que um índice novo
parecer promissor.

---

## 12. O stop-loss que não existia

Durante todo o desenvolvimento, `stop_loss` e `take_profit` foram calculados pelo
Risk Manager, gravados na ordem, persistidos no banco e expostos na API — e
**nunca comparados com preço nenhum**.

Nem no backtest, nem em produção.

### Como isso passou

Um parâmetro inerte não falha. A suíte inteira passava. O `check` dizia "tudo
pronto". O dashboard mostrava os níveis. A única coisa que denunciou foi uma
varredura de 768 combinações para responder outra pergunta, em que mudar
`stop_loss_pct` não alterou **um único** resultado: 480 de 480 pares idênticos.

Testar que um valor é gravado não testa que ele é usado.

### O que isso invalidou

Todas as quedas máximas já medidas eram o retrato de um sistema **sem** stop —
justamente o número que o stop existe para limitar. Com a execução funcionando,
os mesmos dados dão outra resposta:

| Estratégia | Retorno antes → depois | Queda antes → depois |
|---|---|---|
| rsi_reversion | +40,55% → **−16,39%** | 40,5% → **20,5%** |
| ma_crossover | −5,24% → **+6,91%** | 44,3% → **7,7%** |
| macd_trend | +39,53% → **−6,07%** | 26,8% → **11,2%** |
| as três juntas | −8,95% → −16,66% | 46,4% → **23,8%** |

O stop corta a queda pela metade ou mais em todos os casos — e destrói os dois
resultados que pareciam bons. O `+40,55%` do `rsi_reversion` vinha de atravessar
quedas de 40% até a recuperação. Com stop, você sai no fundo e não participa da
volta. Essa troca é real, não é bug: **o stop compra menos queda com menos
retorno**, e os `+40%` nunca foram alcançáveis por quem usa stop.

Só o `ma_crossover` melhorou, e é hoje o único resultado consistente do projeto:
+4,39%, +2,03% e +4,80% nas três janelas, com quedas de 3,5%, 3,8% e 4,4%.

### Abrir a carteira: o que custa aplicar 100% em vez de 45%

A configuração medida acima usava 3 posições de 15% — no máximo 45% do capital
aplicado, com 55% sempre em caixa. Com o limite de posições removido (D19), o
caixa passa a ser o único freio e a carteira enche em 7 posições.

Medido, `ma_crossover` em 1d sobre a janela inteira:

| Posições | Retorno | Queda máxima | Oper/mês | Taxa/mês |
|---|---|---|---|---|
| 3 | +6,91% | **7,7%** | 12 | 0,18% |
| 5 | **+10,80%** | 8,0% | 15 | 0,23% |
| 7 | +9,84% | 8,9% | 16 | 0,24% |
| sem limite | +9,84% | 8,9% | 16 | 0,24% |

Duas confirmações mecânicas: "sem limite" dá exatamente o mesmo que 7 posições,
porque é onde o caixa acaba; e 3 posições com teto de R$25 dá exatamente o mesmo
que 3 posições sem teto, porque em R$150 os 15% valem R$22,50 e o teto nunca
chegava a limitar.

**O que as três janelas mostram, e a janela inteira esconde:**

| Janela | 3 posições | Sem limite | Comprar e segurar |
|---|---|---|---|
| 1 (2023-12→2024-11) | +4,39% (queda 3,5%) | +3,72% (**6,2%**) | +24,2% |
| 2 (2024-11→2025-10) | +2,03% (3,8%) | +1,34% (**5,6%**) | −1,8% |
| 3 (2025-10→2026-09) | +4,80% (4,4%) | **+9,30%** (6,0%) | −20,8% |

Abrir a carteira **aumenta a queda máxima nas três janelas** — de 3,5–4,4% para
5,6–6,2%, cerca de 50% mais. O retorno melhora em **uma** das três, e o ganho de
+2,9 pp na janela inteira vem inteiro dessa janela.

Ambas as configurações seguem positivas nas três janelas, o que é o resultado
robusto e vale para as duas. A escolha entre elas é sobre quanto capital ocioso
se aceita: mais aplicado rendeu mais no agregado, e oscilou mais sempre.

Nota de disciplina: **5 posições foi o melhor número da janela inteira**, com
retorno maior e queda menor que "sem limite". Não é motivo para configurar 5 — é
uma janela, e este projeto já registrou duas vezes o custo de escolher parâmetro
pelo melhor resultado de uma janela (seção 11). O único efeito consistente
nas três é que mais posições aumentam a queda.

### As outras estratégias, na configuração vigente

| Estratégia | Retorno | Queda | Acerto |
|---|---|---|---|
| ma_crossover | **+9,84%** | 8,9% | 38% |
| macd_trend | −4,21% | 12,4% | 33% |
| rsi_reversion | −20,16% | 24,9% | 26% |
| as três juntas | −15,90% | 24,5% | 33% |

`ma_crossover` isolado continua sendo a única escolha que a medição sustenta.

### Stop mais apertado foi melhor, não pior

Contraintuitivo e consistente nas três estratégias medidas:

| Estratégia | stop 2% | stop 5% | stop 15% |
|---|---|---|---|
| rsi_reversion | −13,39% | −24,17% | −36,91% |
| macd_trend | −6,09% | −11,34% | −21,08% |
| as três juntas | −19,31% | −34,87% | −34,84% |

Stop largo deixa a perda correr e ainda aumenta a queda máxima. Piora nos dois
eixos ao mesmo tempo.

### Em produção: stop em software, com limitação conhecida

O Risk Manager passou a comparar cada posição aberta contra os níveis a cada
snapshot do portfólio (60s por padrão) e a emitir o fechamento quando rompem.
O nível vem do **preço médio** da posição, reconstruído do histórico de trades —
o que inclui lançamentos manuais. Uma posição comprada fora do sistema também
passa a ser protegida: o Risk Manager guarda a carteira, não apenas as ordens
que ele originou.

Foi escolhido em vez de mandar uma OCO para a exchange. O que essa escolha
**não** cobre, e precisa estar claro antes de ligar o LIVE:

| | Stop em software | OCO na exchange |
|---|---|---|
| Oscilação de mercado | ✔ | ✔ |
| Processo morre / máquina reinicia | ✘ | ✔ |
| Perda de acesso à API (IP mudou) | ✘ | ✔ |
| Pavio dentro do intervalo de 60s | ✘ | ✔ |

As duas primeiras linhas importam mais do que parecem, e a seção 1 deste
documento já descreve exatamente esse cenário: quando o IP residencial troca, o
sistema perde a exchange e uma posição aberta fica sem saída. **Proteção que
depende do processo estar vivo cobre mercado, não infraestrutura.**

Nota comparativa: o backtest, que lê a mínima do candle, é nesse ponto **mais
severo** que a produção — ele pega o pavio, a produção não. Um backtest que
mostra o stop disparando pode corresponder, na prática, a uma posição que
sobreviveu.

`ccxt_adapter.place_order` continua sem enviar `stopPrice` nem OCO; a proteção é
inteiramente do lado do sistema. Não há trava impedindo o LIVE: a decisão de
subir com essa limitação é do operador, e este é o registro dela.

### Por que o stop vai para caixa, e não para outro ativo

Pergunta que aparece naturalmente: em vez de vender para BRL, o stop não deveria
trocar a posição por USDC ou BTC? Medido, e as duas ideias têm respostas
diferentes.

**Para BTC: não.** O que o BTC/BRL fez depois de cada um dos 160 stops de uma
janela, comparado com um dia qualquer da mesma série:

| Depois de | BTC após um stop | BTC em dia qualquer |
|---|---|---|
| 1 dia | **−0,42%** (negativo em 58%) | +0,06% (49%) |
| 5 dias | **−0,62%** (57%) | +0,11% (49%) |
| 10 dias | **−1,42%** (54%) | +0,58% (47%) |
| 20 dias | +0,24% (49%) | +0,40% (48%) |

Stops disparam quando o mercado cai, e o BTC continua caindo por cerca de dez
dias com mais frequência do que num dia normal. Trocar para BTC converteria uma
perda **encerrada** em uma perda que **continua** — mediana de −1,42% adicionais,
em cima dos 3% do stop.

A razão é estrutural: os 16 pares são todos cripto e caem junto com o BTC. Parar
a perda em SOL e entrar em BTC encerra a parte específica do SOL e mantém a parte
de mercado, que é a maior. **Um stop que mantém a exposição que o disparou não é
um stop.**

**Para USDC: é aposta de câmbio, não medida de risco.** O USDC/BRL é
descorrelacionado do evento (mediana +0,03% em 5 dias após um stop). Ele não
ajuda nem prejudica a saída — apenas troca exposição em real por exposição em
dólar. Na janela com histórico disponível isso teria custado **−4,47%**, porque o
real se fortaleceu.

**E o mecanismo cobraria caro:** duas operações por saída em vez de uma (~0,6% em
vez de ~0,3%), quatro pernas por ciclo de saída e reentrada, três dos dezesseis
ativos sem par direto líquido em USDC — e uma colisão concreta de contabilidade,
porque USDC guardado seria visto pelo Risk Manager como posição, com preço médio
e, portanto, com stop próprio: uma oscilação de 3% no câmbio "stoparia" o caixa.

Se em algum momento o objetivo for não ficar em real, o caminho limpo não é o
swap: é **trocar a moeda de cotação para USDC** e negociar pares em USDC. Aí o
caixa já é dólar por natureza, sem perna extra, sem taxa a mais e sem colisão. A
Binance tem 45 pares USDC líquidos, contra 43 em BRL.

### As três convenções do backtest, todas pessimistas

1. **Stop e alvo no mesmo candle: o stop vence.** O OHLC não diz qual preço veio
   primeiro. Supor o alvo seria escolher o desfecho bom com informação que não
   existe.
2. **Gap conta contra.** Candle que abre abaixo do stop executa na abertura, não
   no nível — é assim que stops machucam de verdade. O alvo, ao contrário,
   executa no nível, sem crédito pelo gap favorável.
3. **Deslizamento nas duas pontas.**

Os níveis protegem a **posição**, não o lote: ao reforçar uma posição são
recalculados sobre o preço médio. Manter o stop do primeiro lote deixaria a parte
nova descoberta, e dois pares de níveis exigiriam fatiar a posição na venda — o
que a exchange não faz em spot.

---

## 13. O portão de capital: auto-ajuste com aval humano

O sistema acompanha o saldo sozinho, e não usa saldo que ninguém autorizou. As
duas coisas ao mesmo tempo, porque resolvem problemas opostos.

### O que travava o auto-ajuste

Dois limites não escalavam com a carteira:

- **Teto absoluto por ordem** (era R$25 fixo). Acima de ~R$167 de patrimônio ele
  passava a mandar, e a ordem parava de crescer. Em R$5.000 o sistema operaria
  1,5% da carteira.
- **Número de posições** (era 3). O capital aplicado travava em 3 × teto,
  independente do saldo.

Os dois passaram a aceitar **ausência de limite**:

| Campo | Vazio significa |
|---|---|
| Valor máximo por ordem | sem teto — o percentual manda e a ordem acompanha o saldo |
| Máximo de posições abertas | sem limite — o caixa limita, porque cada ordem consome dinheiro |

Com 15% por ordem e sem esses tetos, a carteira enche em **7 posições, 100% do
capital autorizado**, em qualquer escala:

| Autorizado | Ordem | Posições | Aplicado |
|---|---|---|---|
| R$150 | R$22,50 | 7 | R$150 (100%) |
| R$650 | R$97,50 | 7 | R$650 (100%) |
| R$2.650 | R$397,50 | 7 | R$2.650 (100%) |

A última ordem é menor que as outras (limitada pelo caixa restante), e é isso que
fecha os 100% sem sobra.

### Por que existe um portão, se o objetivo é usar o saldo

**Um depósito não é uma ordem.** Dinheiro entra numa conta de exchange por muitos
motivos — venda de outro ativo, transferência que ia para outra finalidade,
reserva temporária. Nenhum deles significa "aumente minha exposição em cripto".

O campo **capital autorizado** é o teto de dinheiro que o sistema pode pôr para
trabalhar. Saldo acima dele:

- **não é usado** — nem para dimensionar ordens, nem para financiá-las;
- **gera notificação** pedindo autorização, uma vez por transição.

Autorizar grava um **número**, não desliga o portão. Desligar autorizaria também
todo depósito futuro, que é exatamente o que o portão existe para impedir.
Autorizar é um ato sobre o saldo de hoje, e vai para o `audit_log`.

### Duas armadilhas que o desenho fecha

**Reciclagem.** Se o portão olhasse apenas o caixa livre, vender uma posição e
recomprar outra manteria exposição acima do autorizado. Por isso as posições
abertas **consomem** a autorização: o que pode ser gasto é o autorizado menos o
que já está aplicado.

**Ruído no alerta.** Posição valorizando move o patrimônio para cima sem ninguém
depositar nada. O alerta só sai quando o saldo não autorizado passa da ordem
mínima, e uma vez por transição — a cada 60s o mesmo saldo geraria 1.440
mensagens por dia, e um alerta que chega sempre deixa de ser lido.

### O outro lado: dinheiro parado também é problema

O aviso é ativo, e não uma linha no `check`, justamente porque o modo de falha
silenciosa aqui é o mesmo que já apareceu duas vezes neste projeto: o sistema de
pé, heartbeat verde, dashboard atualizando, e nada acontecendo por um motivo que
ninguém vê. Saldo parado por falta de autorização seria indistinguível de "não há
oportunidade".

Por isso o `check` também imprime o portão, e a tela de Risco mostra uma faixa
com o valor parado e um botão para liberar.

---

## 14. Ambiente e negócio: a fronteira, e por que ela é rígida

O `.env` descreve **a instalação**. O banco guarda **o que negociar e com quanto
risco**. Nenhuma variável mora nos dois lugares, e escrever uma variável de
negócio no `.env` faz o sistema **recusar subir**, listando as chaves.

| | **Ambiente — `.env`** | **Negócio — banco** |
|---|---|---|
| Credenciais da exchange, SMTP, token da Meta | ✔ | |
| `TRADING_MODE`, `LIVE_TRADING_CONFIRMED` | ✔ | |
| `EXCHANGE` | ✔ | |
| Banco, event bus, host/porta da API, CORS, log | ✔ | |
| Moeda de cotação, pares, timeframe, estratégias | | ✔ |
| Cadência de coleta, histórico por par, janela de sinais | | ✔ |
| Critérios de descoberta automática | | ✔ |
| Todos os limites de risco, incluindo o filtro MVRV | | ✔ |
| Parâmetros de simulação (saldo, taxa, slippage) | | ✔ |

### O episódio que tornou isso uma regra

Antes da separação, os limites de risco viviam nos dois lugares: o `.env`
semeava o banco na primeira subida e, daí em diante, o banco vencia — em
silêncio. O `.env` desta instalação pedia ordem máxima de **7 USDT** e no máximo
**2 posições**; o banco, semeado dias antes com os padrões da época, mandava
**50** e **5**. Sete campos divergiam, incluindo os dois circuit breakers. E o
`check` imprimia os números do arquivo, não os que valiam.

Ninguém tinha errado. A configuração é que admitia duas verdades sobre a mesma
quantia de dinheiro real. Configuração duplicada não é redundância.

### Como isso é imposto no código

`RiskSettings` e `TradingSettings` são `BaseModel` puros — não `BaseSettings`.
Não leem ambiente nem por acidente: não existe caminho de código do `.env` até
eles. E `Settings` carrega uma lista das chaves de negócio aposentadas
(`RETIRED_ENV_KEYS`), varre o ambiente e o próprio arquivo, e falha com a lista
do que precisa sair.

Um teste garante que a lista não fica atrás do código: adicionar um campo a
qualquer um dos dois modelos sem registrá-lo na trava quebra a suíte. Sem isso, a
próxima variável de negócio voltaria a passar batida pelo `.env`.

### As duas fronteiras que exigiram decisão

**`TRADING_MODE` é ambiente.** Poderia ser negócio — é a decisão mais de negócio
que existe. Mas ligar dinheiro real deve exigir acesso ao servidor, editar um
arquivo e reiniciar o processo: três coisas que um clique no navegador não
alcança. A dupla confirmação (`TRADING_MODE=live` **e**
`LIVE_TRADING_CONFIRMED=true`) só protege enquanto mora fora da aplicação.

**`EXCHANGE` é ambiente; a moeda de cotação é negócio.** A exchange está amarrada
a qual credencial existe no arquivo — apontar para uma sem chave é erro de
instalação. A moeda de cotação define o universo de pares e é escolha de quem
opera.

### Como mudar cada coisa

- **Negócio** → telas **Configurações** e **Risco**. Exigem `confirm`, validam o
  resultado do merge (um campo isolado pode ser válido e tornar o conjunto
  incoerente), valem imediatamente sem reiniciar, e vão ao `audit_log` com o
  antes e o depois.
- **Ambiente** → editar o `.env` e reiniciar. Sem rastro, por natureza: é por isso
  que só ambiente mora lá.

O `check` imprime `negocio : banco` quando leu a linha existente, e avisa quando
acabou de criá-la com os padrões de fábrica.

---

## 15. Antes de ligar o LIVE

> ⚠️ **Leia a seção 11 antes.** O stop-loss existe em software, no Risk Manager,
> e **não** na exchange. Ele não age se o processo morrer, se a máquina
> reiniciar, ou se o sistema perder acesso à API — e é justamente nesse último
> cenário que uma posição aberta fica sem saída. Subir em LIVE com essa limitação
> é uma decisão consciente, não um esquecimento.

Uma sequência, não uma escolha:

1. Backtest da estratégia com dados históricos.
2. `dry_run` por **semanas**, com o dashboard acompanhado de perto.
3. `testnet`, para validar a integração real com a API da exchange.
4. **Teste deliberado do circuit breaker** — force o cenário de perda e confirme
   que a paralisação acontece de verdade.
5. **Teste deliberado do stop-loss** — abra uma posição em `testnet` e confirme
   que ela fecha sozinha ao romper o nível. Este passo não existia, e é por isso
   que o defeito da seção 11 sobreviveu ao desenvolvimento inteiro.
6. Chaves da exchange sem permissão de saque, com whitelist de IP.
7. `live` com limites conservadores e valores pequenos.

Os passos 4 e 5 são os mais fáceis de pular e os mais caros de ter pulado. Ambos
têm a mesma forma: **provocar a proteção e verificar que ela agiu.** Ler que ela
está configurada não é a mesma coisa — foi exatamente essa diferença que deixou o
stop inerte por todo o projeto.

---

## Aviso legal

Negociação de criptomoedas tem implicações fiscais no Brasil (ganho de capital e
declaração à Receita Federal acima de certos limites de venda mensal). O histórico
completo e exportável em CSV existe para facilitar essa apuração, mas isto não é
aconselhamento jurídico, fiscal ou financeiro — consulte um contador especializado
em criptoativos.
