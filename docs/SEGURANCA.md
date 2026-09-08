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
alavancados. Para excluir um ativo específico use `DISCOVERY_EXCLUDE_ASSETS`,
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

**Stop-loss e take-profit são anexados pelo Risk Manager**, não pela estratégia.
Uma estratégia nova, escrita meses depois, não tem como esquecer de definir stop:
ela nem participa dessa etapa.

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
ordem. Em R$100, mesmo subindo `RISK_MAX_ORDER_PCT_PORTFOLIO` para 55%, quem
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

```dotenv
RISK_MAX_ORDER_NOTIONAL=7
RISK_MAX_ORDER_PCT_PORTFOLIO=0.35
RISK_MAX_ASSET_EXPOSURE_PCT=0.40
RISK_MAX_OPEN_POSITIONS=2
RISK_MIN_ORDER_NOTIONAL=6
RISK_DAILY_LOSS_LIMIT_PCT=0.08
```

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

## 9. Antes de ligar o LIVE

Uma sequência, não uma escolha:

1. Backtest da estratégia com dados históricos.
2. `dry_run` por **semanas**, com o dashboard acompanhado de perto.
3. `testnet`, para validar a integração real com a API da exchange.
4. **Teste deliberado do circuit breaker** — force o cenário de perda e confirme
   que a paralisação acontece de verdade.
5. Chaves da exchange sem permissão de saque, com whitelist de IP.
6. `live` com limites conservadores e valores pequenos.

O passo 4 é o mais fácil de pular e o mais caro de ter pulado.

---

## Aviso legal

Negociação de criptomoedas tem implicações fiscais no Brasil (ganho de capital e
declaração à Receita Federal acima de certos limites de venda mensal). O histórico
completo e exportável em CSV existe para facilitar essa apuração, mas isto não é
aconselhamento jurídico, fiscal ou financeiro — consulte um contador especializado
em criptoativos.
