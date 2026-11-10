"""Backtest de carteira: todos os pares competindo pelo mesmo dinheiro.

O `BacktestEngine` de um par so responde "essa estrategia funciona nesse par".
Ele nao responde a pergunta que aparece quando se liberam 16 pares: **quantas
operacoes isso gera de fato, e quanto a taxa come?**

Somar 16 execucoes independentes daria a resposta errada, e para cima:

- cada execucao usaria a carteira inteira, entao 16 delas gastariam 16x o
  dinheiro que existe;
- `max_open_positions` nao seria respeitado, porque cada par ignoraria os outros;
- a exposicao por ativo tambem nao, pela mesma razao.

Aqui existe **um** `PaperBroker` e **um** `RiskEngine` para todos os pares. Os
candles sao percorridos em ordem cronologica global, e cada sinal disputa o mesmo
caixa com os demais.

Para medir o sistema que RODA, e nao outro, o motor segue a configuracao de
producao em cada ponto em que ela decide algo (auditoria de 09/11/2026):

- **Selecao de sinais.** `signal_batch_window_seconds` espelha o campo de mesmo
  nome de `TradingSettings`, com o mesmo padrao (0). Com 0, cada sinal e decidido
  sozinho, na ordem de chegada, contra o estado ja atualizado pelos anteriores --
  o motor decidia sempre o instante inteiro por confianca, o mecanismo que a
  producao desligou em `0844d41` (SEGURANCA §9). "Ordem de chegada", aqui, e a
  ordem em que o Market Data publica os candles: a ordem dos pares no dicionario
  recebido (a de `SYMBOLS`, ou a da descoberta, que o Market Data percorre em
  sequencia) e, dentro do par, a ordem das estrategias, que e como o Strategy
  Agent publica. Quem monta `candles_by_symbol` precisa respeitar essa ordem.
- **Stop e alvo** pela leitura de 60 s, com as folgas de `protecao.py`.
- **Cooldown e "agora" do risco** no fechamento do candle, quando a producao
  decide; a saida de protecao fica carimbada na abertura (aconteceu durante o
  candle).
- **Periodo e comprar-e-segurar** a partir do primeiro instante operavel.

**Vies de sobrevivencia.** O motor nao sabe quais pares existiam em cada data:
ele recebe o universo de quem chama, e hoje esse universo e a lista de HOJE
(`SYMBOLS`, ou os pares liquidos que a descoberta acha agora). Par deslistado ou
que perdeu liquidez no periodo nao entra, e a carteira medida so tem
sobreviventes -- retorno e comprar-e-segurar tendem a sair otimistas. Sem dado
historico de listagem nao ha reconstrucao; o que o dado publico permite, o
motor faz:

- o primeiro candle de cada par marca a entrada dele: par listado no meio da
  janela comeca a operar quando completa o aquecimento e fica declarado em
  `pares_tardios`, fora do comprar-e-segurar;
- par cuja serie ACABA antes do fim (deslistado, numa lista historica) fica
  declarado em `pares_encerrados`. O que sobrar nele fica avaliado no ultimo
  fechamento ate o fim -- o motor nao inventa o preco da deslistagem;
- sem `universo_fixado_em` (ou com uma data depois do inicio da janela), o
  resultado leva `vies_de_sobrevivencia` e o log
  `portfolio_backtest.vies_de_sobrevivencia` diz o mesmo.

Como medir o efeito, quando houver a lista de uma data passada (o sistema nao
guarda historico de universo; quem tiver a lista fornece): (1) fixe os pares
negociaveis numa data D anterior a janela -- inclusive os que depois sairam --,
com os candles que existirem deles; (2) rode a MESMA janela e configuracao com
essa lista (`universo_fixado_em=D`) e com a lista de hoje; (3) a diferenca de
`total_return_pct` e de `buy_and_hold_pct` entre as duas execucoes e a
estimativa do vies, e `pares_encerrados` diz quanto dela veio de pares que
pararam no meio. Uma unica janela e uma medida, nao uma lei: repetir em datas D
diferentes.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd

from ..config import RiskSettings, TradingSettings
from ..domain.enums import ExchangeName, OrderStatus, OrderType, RiskDecision, Side, SignalDirection
from ..domain.models import Candle, OrderRequest
from ..exchanges.base import timeframe_seconds
from ..exchanges.paper import PaperBroker
from ..logging_setup import get_logger
from ..onchain import reading_from_series
from ..risk.rules import PortfolioState, RiskEngine
from ..strategies import MarketFrame, Strategy
from .engine import liquido_e_taxa, preenchimento
from .protecao import FOLGAS_PADRAO, FolgasDeProtecao, saida_de_protecao

log = get_logger(__name__)

#: Padrao da producao para a janela de lote (0 = cada sinal sozinho, por chegada).
JANELA_DE_LOTE_DA_PRODUCAO: float = TradingSettings.model_fields[
    "signal_batch_window_seconds"
].default


@dataclass
class PortfolioTrade:
    timestamp: datetime
    symbol: str
    strategy: str
    side: str
    quantity: Decimal
    price: Decimal
    notional: Decimal
    fee: Decimal
    confidence: float = 0.0
    """Confianca do sinal que originou a ordem.

    Registrada para permitir a pergunta que decide se a selecao por confianca
    faz sentido: confianca alta de fato prediz resultado melhor?
    """

    realized_pnl: Decimal | None = None

    exit_reason: str | None = None
    """Por que a posicao fechou: `stop`, `alvo` ou `estrategia`.

    Sem isso nao da para responder a pergunta que decide o valor do stop: das
    saidas, quantas foram protecao disparando e quantas foram a estrategia
    mandando sair?
    """

    fee_no_ativo: Decimal = Decimal(0)
    """Taxa cobrada no ativo base, na unidade dele (compra na Binance sem BNB).

    `fee` fica sempre na moeda de cotacao, para as taxas somarem. Este campo e o
    que permite reproduzir o trade como a producao grava: `quantity` bruta e a
    taxa no ativo, com o saldo entrando liquido.
    """


@dataclass
class PortfolioBacktestResult:
    quote_currency: str
    start: datetime
    end: datetime
    initial_balance: Decimal
    final_value: Decimal
    symbols: list[str]
    trades: list[PortfolioTrade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    signals_generated: int = 0
    signals_rejected: int = 0
    rejection_reasons: Counter = field(default_factory=Counter)
    trades_per_symbol: Counter = field(default_factory=Counter)
    exits: Counter = field(default_factory=Counter)
    """Contagem de saidas por motivo: `stop`, `alvo`, `estrategia`."""

    #: Preco no inicio e no fim da janela, por par. Base do comprar-e-segurar.
    #: `first_prices` e o fechamento em `start`, a mesma data para todos; par que
    #: so entrou depois (`pares_tardios`) nao tem preco ali e fica de fora.
    first_prices: dict[str, Decimal] = field(default_factory=dict)
    last_prices: dict[str, Decimal] = field(default_factory=dict)

    pares_descartados: dict[str, str] = field(default_factory=dict)
    """Par recebido que nao entrou, com o motivo (historico curto demais)."""

    pares_tardios: dict[str, datetime] = field(default_factory=dict)
    """Par que so passou a operar depois de `start`, com o instante em que entrou.

    O motor nao sabe que pares existiam em cada data; isto so declara quais
    comecaram depois dos outros, para o leitor saber que nao operaram o periodo
    inteiro e nao entram no comprar-e-segurar.
    """

    pares_encerrados: dict[str, datetime] = field(default_factory=dict)
    """Par cuja serie acaba antes do fim da janela, com o ultimo candle dele.

    Numa lista historica e o par deslistado: o que sobrar nele fica avaliado no
    ultimo fechamento ate o fim, porque o motor nao inventa o preco da saida.
    """

    vies_de_sobrevivencia: str | None = None
    """Aviso de vies de sobrevivencia, ou None quando o universo e historico.

    Preenchido quando a lista de pares nao foi fixada numa data ate o inicio da
    janela (`universo_fixado_em`): a carteira medida so tem os pares que
    sobreviveram ate a data da lista. Ver o docstring do modulo.
    """

    @property
    def buy_and_hold_pct(self) -> float:
        """Comprar e segurar em partes iguais nos mesmos pares, no mesmo periodo.

        E a referencia que decide se valeu operar. Sem ela, um "+4,88%" parece
        bom -- e pode ser metade do que o mercado entregou sozinho.
        """
        pares = [s for s in self.first_prices if s in self.last_prices]
        if not pares:
            return 0.0
        retornos = [
            (self.last_prices[s] - self.first_prices[s]) / self.first_prices[s]
            for s in pares
            if self.first_prices[s] > 0
        ]
        if not retornos:
            return 0.0
        return float(sum(retornos) / len(retornos))

    @property
    def beat_the_market(self) -> bool:
        return self.total_return_pct > self.buy_and_hold_pct

    @property
    def days(self) -> float:
        span = (self.end - self.start).total_seconds() / 86400
        return max(span, 1 / 24)

    @property
    def total_fees(self) -> Decimal:
        return sum((t.fee for t in self.trades), start=Decimal(0))

    @property
    def trades_per_month(self) -> float:
        return len(self.trades) / self.days * 30

    @property
    def fees_per_month(self) -> Decimal:
        """Taxa mensal projetada, na moeda de cotacao."""
        if not self.trades:
            return Decimal(0)
        return self.total_fees / Decimal(str(self.days)) * 30

    @property
    def fees_pct_of_capital_per_month(self) -> float:
        """A metrica que importa: quanto do patrimonio a taxa consome por mes."""
        if self.initial_balance <= 0:
            return 0.0
        return float(self.fees_per_month / self.initial_balance)

    @property
    def total_return_pct(self) -> float:
        if self.initial_balance <= 0:
            return 0.0
        return float((self.final_value - self.initial_balance) / self.initial_balance)

    @property
    def max_drawdown_pct(self) -> float:
        peak = Decimal(0)
        worst = 0.0
        for _, value in self.equity_curve:
            peak = max(peak, value)
            if peak > 0:
                worst = max(worst, float((peak - value) / peak))
        return worst

    @property
    def closed_trades(self) -> list[PortfolioTrade]:
        return [t for t in self.trades if t.realized_pnl is not None]

    @property
    def win_rate(self) -> float:
        closed = self.closed_trades
        if not closed:
            return 0.0
        return sum(1 for t in closed if (t.realized_pnl or Decimal(0)) > 0) / len(closed)

    @property
    def realized_pnl(self) -> Decimal:
        return sum((t.realized_pnl or Decimal(0) for t in self.closed_trades), start=Decimal(0))


class PortfolioBacktestEngine:
    """Percorre varios pares sobre uma carteira unica."""

    def __init__(
        self,
        strategies: list[Strategy],
        risk_limits: RiskSettings,
        *,
        quote_currency: str = "USDT",
        initial_balance: Decimal = Decimal("1000"),
        fee_pct: Decimal = Decimal("0.001"),
        slippage_pct: Decimal = Decimal("0.0005"),
        lookback: int = 500,
        min_warmup: int = 0,
        mvrv_series: list[tuple[date, float]] | None = None,
        signal_batch_window_seconds: float = JANELA_DE_LOTE_DA_PRODUCAO,
        folgas: FolgasDeProtecao = FOLGAS_PADRAO,
        universo_fixado_em: date | None = None,
    ) -> None:
        self.strategies = strategies
        # Data em que a lista de pares de `run` foi fixada. None = a lista e a
        # da configuracao atual (o caso de hoje), e o resultado leva o aviso de
        # vies de sobrevivencia; so uma data ate o inicio da janela o tira.
        self.universo_fixado_em = universo_fixado_em
        # Espelha `TradingSettings.signal_batch_window_seconds`: 0 decide cada
        # sinal sozinho, por ordem de chegada (o padrao de producao); acima de 0,
        # o instante inteiro vira um lote decidido por confianca. Passe o valor
        # da configuracao que se quer medir.
        if signal_batch_window_seconds < 0:
            raise ValueError("signal_batch_window_seconds nao pode ser negativo")
        self.signal_batch_window_seconds = signal_batch_window_seconds
        self.folgas = folgas
        self.limits = risk_limits
        self.engine = RiskEngine(risk_limits, quote_currency)
        self.quote_currency = quote_currency
        self.initial_balance = initial_balance
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct
        self.lookback = lookback
        # Serie MVRV opcional. Passada pronta em vez de buscada aqui para o
        # backtest nao depender de rede -- e o percentil e calculado so com o
        # passado de cada instante, senao o backtest saberia o futuro.
        self.mvrv_series = mvrv_series or []
        # `min_warmup` alinha o inicio entre execucoes diferentes. E necessario
        # para comparar estrategias entre si: `macd_trend` exige 105 candles de
        # aquecimento e `ma_crossover` 26, entao sem alinhar cada uma comeca a
        # operar -- e a medir o comprar-e-segurar -- num ponto distinto da serie.
        # Em candles diarios isso foi a diferenca entre um benchmark de 56% e um
        # de 1,7% na MESMA janela, e fez estrategias parecerem vencer o mercado.
        self._warmup = max(
            max((s.min_candles for s in strategies), default=50), min_warmup
        )

    def _chave_de_ativo(self, symbol: str) -> str:
        """Chave de preco, posicao, base de custo e protecao para um par.

        Mesmo contrato de `MarketDataAgent._chave_de_preco`, e pelo mesmo motivo.
        `symbol.partition("/")[0]` -- so o ativo base -- fazia BTC/USDC e
        BTC/BRL gravarem na MESMA chave "BTC", e o ultimo par do instante
        vencia. Foi a QUARTA divergencia backtest/producao encontrada neste
        projeto: producao aprendeu a nao misturar unidades (defeito 10 do
        gauntlet) e o backtest nao recebeu a licao.

        O dano medido nao era um numero um pouco errado, era o backtest
        emudecer: com BTC/USDC e BTC/BRL na lista, a compra aprovada em USDC
        chegava ao broker com o preco em BRL e era recusada por saldo
        insuficiente -- 0 trades, retorno 0,00% e `beat_the_market` verdadeiro,
        sem uma linha dizendo que nada aconteceu.

        O ativo base continua sendo a chave do par cotado NA MOEDA DE COTACAO,
        porque e por ativo que o `PaperBroker` guarda saldo e que a carteira e
        avaliada contra o caixa. O par divergente vai para o nome COMPLETO: nao
        colide, e o `RiskEngine` -- que ja recusa ordem cuja cotacao difere da
        configurada -- continua sendo quem barra a ordem. Um par a mais na
        lista, cotado em moeda que o sistema nao usa, passa a ser inofensivo em
        vez de fatal.
        """
        base, _, quote = symbol.partition("/")
        if quote.upper() == self.quote_currency.upper():
            return base
        return symbol

    async def run(
        self, candles_by_symbol: dict[str, list[Candle]]
    ) -> PortfolioBacktestResult:
        # A ordem de `candles_by_symbol` e a ordem de chegada dos sinais (ver o
        # docstring do modulo); `frames` a preserva.
        frames: dict[str, pd.DataFrame] = {}
        referencias: dict[str, Candle] = {}
        descartados: dict[str, str] = {}
        minimo = self._warmup + 2
        for symbol, candles in candles_by_symbol.items():
            fechados = [c for c in candles if c.closed]
            if len(fechados) <= minimo:
                # Antes saia em silencio: o leitor via 15 pares no resultado e
                # nao sabia que tinha pedido 16.
                descartados[symbol] = (
                    f"historico curto: {len(fechados)} candles fechados, "
                    f"o aquecimento pede mais de {minimo}"
                )
                continue
            frames[symbol] = _to_frame(fechados)
            # Por par, e nao "o primeiro candle do primeiro par": esse levantava
            # IndexError com a primeira lista vazia e emprestava exchange e
            # timeframe de um par que podia ter sido descartado.
            referencias[symbol] = fechados[0]
        if descartados:
            log.warning("portfolio_backtest.pares_descartados", pares=descartados)
        if not frames:
            raise ValueError("historico insuficiente em todos os pares")

        # Linha do tempo unica: os pares compartilham a carteira, entao precisam
        # ser avaliados na ordem em que os candles realmente fecharam.
        timeline = sorted({ts for frame in frames.values() for ts in frame.index})
        duracao = timedelta(
            seconds=timeframe_seconds(next(iter(referencias.values())).timeframe)
        )

        # Primeiro instante em que cada par pode operar: o candle de indice
        # `warmup` DELE. O periodo medido comeca no primeiro desses instantes --
        # contar o aquecimento em `days` subestimava taxa/mes e operacoes/mes
        # (66 dias de janela + 26 de aquecimento = 28% a menos). Par cujo
        # primeiro instante vem depois entrou tarde: opera a partir dali, mas
        # fica declarado e fora do comprar-e-segurar.
        entrada = {symbol: frame.index[self._warmup] for symbol, frame in frames.items()}
        inicio = min(entrada.values())
        tardios = {
            symbol: momento.to_pydatetime()
            for symbol, momento in entrada.items()
            if momento > inicio
        }
        if tardios:
            log.warning(
                "portfolio_backtest.pares_tardios",
                pares={s: m.isoformat() for s, m in tardios.items()},
                detail="entraram depois do inicio; fora do comprar-e-segurar",
            )
        # O outro lado da mesma linha do tempo: serie que termina antes do fim.
        encerrados = {
            symbol: frame.index[-1].to_pydatetime()
            for symbol, frame in frames.items()
            if frame.index[-1] < timeline[-1]
        }
        if encerrados:
            log.warning(
                "portfolio_backtest.pares_encerrados",
                pares={s: m.isoformat() for s, m in encerrados.items()},
                detail="a serie acaba antes do fim; o que sobrar fica no ultimo fechamento",
            )
        vies = self._aviso_de_sobrevivencia(inicio)
        if vies is not None:
            log.warning(
                "portfolio_backtest.vies_de_sobrevivencia",
                universo_fixado_em=(
                    self.universo_fixado_em.isoformat() if self.universo_fixado_em else None
                ),
                inicio=inicio.isoformat(),
                pares=len(frames),
                detail=vies,
            )

        broker = PaperBroker(
            quote_currency=self.quote_currency,
            initial_balance=self.initial_balance,
            fee_pct=self.fee_pct,
            slippage_pct=self.slippage_pct,
        )

        result = PortfolioBacktestResult(
            quote_currency=self.quote_currency,
            start=inicio,
            end=timeline[-1],
            initial_balance=self.initial_balance,
            final_value=self.initial_balance,
            symbols=sorted(frames),
            pares_descartados=descartados,
            pares_tardios=tardios,
            pares_encerrados=encerrados,
            vies_de_sobrevivencia=vies,
        )

        # Comprar-e-segurar numa data COMUM: o fechamento em `inicio`, o mesmo
        # instante em que a estrategia pode operar pela primeira vez. Antes cada
        # par usava o proprio indice `warmup`, e um par listado no meio da janela
        # entrava na media com outro periodo (12 meses somados a 5).
        for symbol, frame in frames.items():
            if symbol not in tardios:
                result.first_prices[symbol] = Decimal(str(frame.at[inicio, "close"]))
            result.last_prices[symbol] = Decimal(str(frame["close"].iloc[-1]))

        prices: dict[str, Decimal] = {}
        cost_basis: dict[str, Decimal] = {}
        last_order_at: dict[str, datetime] = {}
        protection: dict[str, tuple[Decimal, Decimal]] = {}
        order_seq = 0

        for now in timeline:
            # 1) Atualiza precos de tudo que fechou candle neste instante.
            for symbol, frame in frames.items():
                if now not in frame.index:
                    continue
                base = self._chave_de_ativo(symbol)
                price = Decimal(str(frame.at[now, "close"]))
                prices[base] = price
                broker.set_price(base, price)
            if now < inicio:
                # Aquecimento: nenhum par pode operar, nao ha posicao a proteger
                # e o patrimonio e o inicial. Nada a medir.
                continue
            # A producao decide sobre o candle no FECHAMENTO dele.
            fechamento = now + duracao

            # 2) Protecao ANTES de qualquer decisao nova.
            #    Um stop que disparou libera caixa e encerra a exposicao; avaliar
            #    sinais antes dele daria ao sistema um dinheiro que ele nao tinha
            #    e uma posicao que ja devia estar fechada.
            order_seq = await self._apply_protective_exits(
                frames, now, broker, cost_basis, protection, last_order_at, result, order_seq
            )

            # 3) Sinais deste instante, na ordem de chegada da producao: pares
            #    na ordem recebida, estrategias na ordem configurada.
            sinais = []
            for symbol, frame in frames.items():
                if now not in frame.index:
                    continue
                position = frame.index.get_loc(now)
                if position < self._warmup:
                    continue

                window = frame.iloc[max(0, position + 1 - self.lookback) : position + 1]
                referencia = referencias[symbol]
                market = MarketFrame(
                    exchange=referencia.exchange,
                    symbol=symbol,
                    timeframe=referencia.timeframe,
                    frame=window,
                )
                for strategy in self.strategies:
                    signal = strategy.evaluate(market)
                    if signal is not None:
                        sinais.append(signal)

            # 4) Decide e executa. Janela 0 (producao): cada sinal e um lote de
            #    um, decidido contra o estado que ja inclui as ordens dos
            #    anteriores. Janela > 0: o instante inteiro num lote, aberturas
            #    por confianca (`RiskEngine.evaluate_batch`).
            if sinais:
                result.signals_generated += len(sinais)
                if self.signal_batch_window_seconds > 0:
                    lotes = [sinais]
                else:
                    lotes = [[signal] for signal in sinais]
                for lote in lotes:
                    for signal, assessment in await self._decide_batch(
                        lote, broker, prices, last_order_at, result, now, fechamento
                    ):
                        order_seq += 1
                        await self._execute(
                            signal, assessment, broker, prices, cost_basis,
                            protection, last_order_at, result, now, order_seq,
                            fechamento,
                        )

            # 5) Patrimonio ao fim de TODO instante. A curva era amostrada 1 em
            #    4 ("16 pares x milhares de candles"), mas a linha do tempo e a
            #    uniao dos instantes, nao pares x candles: 365 pontos por ano em
            #    1d. A queda maxima e calculada sobre esta curva, e tres stops
            #    seguidos entre duas amostras sumiam dela.
            equity = await _equity(broker, prices, self.quote_currency)
            result.equity_curve.append((now, equity))

        result.final_value = await _equity(broker, prices, self.quote_currency)
        log.info(
            "portfolio_backtest.finished",
            symbols=len(frames),
            trades=len(result.trades),
            return_pct=round(result.total_return_pct, 4),
        )
        return result

    def _aviso_de_sobrevivencia(self, inicio: datetime) -> str | None:
        """Texto do aviso de vies de sobrevivencia, ou None com universo historico.

        Uma lista fixada DEPOIS do inicio da janela ainda escolhe pelos pares que
        sobreviveram ate a data dela, entao so uma data ate o inicio tira o aviso.
        """
        fixado = self.universo_fixado_em
        if fixado is not None and fixado <= inicio.date():
            return None
        origem = (
            "a da configuracao atual (SYMBOLS ou a descoberta de hoje)"
            if fixado is None
            else f"fixada em {fixado.isoformat()}, depois do inicio da janela"
        )
        return (
            f"vies de sobrevivencia: a lista de pares e {origem}, nao a que existia "
            f"em {inicio.date().isoformat()}. Par deslistado ou que perdeu liquidez no "
            "periodo nao entra, e retorno e comprar-e-segurar tendem a sair otimistas. "
            "Como medir: docstring de crypto_traders.backtest.portfolio."
        )

    async def _apply_protective_exits(
        self, frames, now, broker, cost_basis, protection, last_order_at, result, order_seq
    ) -> int:
        """Executa stop-loss e take-profit contra o candle que acabou de fechar.

        Este passo **nao existia**, e a ausencia dele nao aparecia como erro: os
        niveis eram calculados pelo Risk Manager, gravados na ordem e nunca
        comparados com preco nenhum. Toda posicao andava ate a estrategia mandar
        sair, e por isso as quedas maximas medidas (37%, 46%) eram o retrato de
        um sistema **sem** stop -- justamente o numero que o stop existe para
        limitar.

        A regra de disparo e preco e `protecao.saida_de_protecao`, a mesma do
        motor de um par: stop vence empate, gap conta contra, e as folgas que
        reproduzem a leitura de 60 s da producao. O "todas pessimistas" que este
        docstring afirmava nao valia: stop no nivel exato e alvo no pavio eram
        otimistas contra o ticker (ver o docstring de `protecao.py`, com a
        medicao). O deslizamento do `PaperBroker` vem por cima: a ordem sai como
        LIMIT no preco calculado e ele aplica o deslizamento contra o operador.
        """
        balances = await broker.fetch_balances()
        for symbol, frame in frames.items():
            if now not in frame.index:
                continue
            base = self._chave_de_ativo(symbol)
            niveis = protection.get(base)
            held = balances.get(base, Decimal(0))
            if niveis is None or held <= 0:
                continue

            stop, alvo = niveis
            disparo = saida_de_protecao(
                Decimal(str(frame.at[now, "open"])),
                Decimal(str(frame.at[now, "high"])),
                Decimal(str(frame.at[now, "low"])),
                stop,
                alvo,
                self.folgas,
            )
            if disparo is None:
                continue
            motivo, fill = disparo

            order_seq += 1
            pedido = OrderRequest(
                client_order_id=f"pbt-{motivo}-{order_seq}",
                signal_id=None,
                risk_event_id=f"protecao-{order_seq}",
                exchange=ExchangeName.PAPER,
                symbol=symbol,
                side=Side.SELL,
                order_type=OrderType.LIMIT,
                price=fill,
                quantity=held,
                notional=held * fill,
                strategy="protecao",
            )
            order = await preenchimento(broker.place_order(pedido), pedido)
            # O `PaperBroker` passa a cotar o ativo no preco da ultima ordem (o
            # nivel do stop). Uma recompra no fechamento deste candle -- outra
            # estrategia, cooldown ja vencido -- seria preenchida ali, e nao no
            # fechamento em que o risco a dimensionou. Devolve o fechamento.
            broker.set_price(base, Decimal(str(frame.at[now, "close"])))
            if order.status is not OrderStatus.FILLED:
                continue

            preco = order.average_price or fill
            medio = cost_basis.get(base, Decimal(0)) / held if held > 0 else Decimal(0)
            vendido, taxa = liquido_e_taxa(order, Side.SELL, base, preco)
            realizado = order.filled_quantity * preco - taxa - vendido * medio

            # A posicao foi zerada, salvo a poeira que o truncamento ao passo do
            # par deixa (como no live): ela fica com o custo dela e sem protecao.
            sobra = max(Decimal(0), held - vendido)
            cost_basis[base] = sobra * medio
            protection.pop(base, None)
            # O cooldown vale para a reentrada: um stop que dispara e o momento
            # em que menos se quer recomprar no candle seguinte. Carimbo na
            # ABERTURA, o instante mais cedo em que a saida pode ter ocorrido:
            # com o mesmo carimbo do sinal do fechamento, uma reentrada no mesmo
            # candle era recusada por cooldown (elapsed 0), e em producao o stop
            # vem horas antes do fechamento em 1d.
            last_order_at[symbol] = now
            balances[base] = sobra

            result.trades.append(
                PortfolioTrade(
                    timestamp=now,
                    symbol=symbol,
                    strategy="protecao",
                    side=str(Side.SELL),
                    quantity=order.filled_quantity,
                    price=preco,
                    notional=order.filled_quantity * preco,
                    fee=taxa,
                    realized_pnl=realizado,
                    exit_reason=motivo,
                )
            )
            result.trades_per_symbol[symbol] += 1
            result.exits[motivo] += 1
        return order_seq

    async def _decide_batch(
        self, batch, broker, prices, last_order_at, result, now, fechamento
    ) -> list:
        """Aplica as regras de risco ao lote e devolve so os aprovados.

        `now` (abertura do candle) so serve a leitura do MVRV, como antes;
        `fechamento` e o "agora" do risco -- cooldown incluido.
        """
        balances = await broker.fetch_balances()
        cash = balances.get(self.quote_currency, Decimal(0))
        positions = {
            asset: amount for asset, amount in balances.items() if asset != self.quote_currency
        }
        total = cash + sum(
            (amount * prices.get(asset, Decimal(0)) for asset, amount in positions.items()),
            start=Decimal(0),
        )
        leitura = (
            reading_from_series(self.mvrv_series, now) if self.mvrv_series else None
        )
        state = PortfolioState(
            total_value=total,
            cash=cash,
            positions=positions,
            prices=dict(prices),
            last_order_at=dict(last_order_at),
            mvrv_percentile=leitura.percentile if leitura else None,
        )

        aprovados = []
        for signal, assessment in self.engine.evaluate_batch(batch, state, now=fechamento):
            if assessment.decision is RiskDecision.REJECTED:
                result.signals_rejected += 1
                for reason in assessment.reasons:
                    result.rejection_reasons[reason.split(":")[0]] += 1
            elif assessment.approved_quantity:
                aprovados.append((signal, assessment))
        return aprovados

    async def _execute(
        self, signal, assessment, broker, prices, cost_basis, protection,
        last_order_at, result, now, order_seq, fechamento=None,
    ) -> None:
        # `fechamento`: carimbo do cooldown (ver `run`). Sem ele, `now` -- quem
        # dita uma sequencia de ordens a mao (teste de PnL) nao tem candle.
        fechamento = fechamento or now
        base = self._chave_de_ativo(signal.symbol)
        side = Side.BUY if signal.direction is SignalDirection.LONG else Side.SELL
        request = OrderRequest(
            client_order_id=f"pbt-{order_seq}",
            signal_id=signal.id,
            risk_event_id=assessment.id,
            exchange=ExchangeName.PAPER,
            symbol=signal.symbol,
            side=side,
            order_type=OrderType.MARKET,
            quantity=assessment.approved_quantity,
            notional=assessment.approved_notional or Decimal(1),
            stop_loss=assessment.stop_loss,
            take_profit=assessment.take_profit,
            strategy=signal.strategy,
        )
        order = await preenchimento(broker.place_order(request), request)
        if order.status is not OrderStatus.FILLED:
            return

        fill = order.average_price or signal.reference_price
        quantity = order.filled_quantity
        no_saldo, taxa = liquido_e_taxa(order, side, base, fill)
        last_order_at[signal.symbol] = fechamento

        realized: Decimal | None = None
        held = (await broker.fetch_balances()).get(base, Decimal(0))
        if side is Side.BUY:
            # Taxa no ativo recebido (Binance sem BNB): o saldo ja entrou liquido
            # e o custo e o bruto pago -- a taxa fica embutida no medio.
            cost_basis[base] = cost_basis.get(base, Decimal(0)) + no_saldo * fill + taxa
            # Os niveis protegem a POSICAO, nao o lote: ao reforcar uma posicao
            # existente eles sao recalculados sobre o preco medio. Manter o stop
            # do primeiro lote deixaria a parte nova sem protecao, e criar um
            # segundo par de niveis exigiria fatiar a posicao na venda -- o que a
            # exchange nao faz num mercado spot.
            medio = cost_basis[base] / held if held > 0 else fill
            protection[base] = (
                medio * (Decimal(1) - Decimal(str(self.limits.stop_loss_pct))),
                medio * (Decimal(1) + Decimal(str(self.limits.take_profit_pct))),
            )
        else:
            previous = held + no_saldo
            average = (cost_basis.get(base, Decimal(0)) / previous) if previous > 0 else Decimal(0)
            realized = quantity * fill - taxa - no_saldo * average
            cost_basis[base] = cost_basis.get(base, Decimal(0)) - no_saldo * average
            if held <= 0:
                protection.pop(base, None)
            result.exits["estrategia"] += 1

        result.trades.append(
            PortfolioTrade(
                timestamp=now,
                symbol=signal.symbol,
                strategy=signal.strategy,
                side=str(side),
                quantity=quantity,
                price=fill,
                notional=quantity * fill,
                fee=taxa,
                confidence=signal.confidence,
                realized_pnl=realized,
                exit_reason="estrategia" if side is Side.SELL else None,
                fee_no_ativo=order.fee if order.fee_currency == base else Decimal(0),
            )
        )
        result.trades_per_symbol[signal.symbol] += 1


def _to_frame(candles: list[Candle]) -> pd.DataFrame:
    closed = [c for c in candles if c.closed]
    return pd.DataFrame(
        {
            "open": [float(c.open) for c in closed],
            "high": [float(c.high) for c in closed],
            "low": [float(c.low) for c in closed],
            "close": [float(c.close) for c in closed],
            "volume": [float(c.volume) for c in closed],
        },
        index=pd.DatetimeIndex([c.open_time for c in closed], name="open_time"),
    ).sort_index()


async def _equity(broker: PaperBroker, prices: dict[str, Decimal], quote: str) -> Decimal:
    balances = await broker.fetch_balances()
    total = balances.get(quote, Decimal(0))
    for asset, amount in balances.items():
        if asset != quote:
            total += amount * prices.get(asset, Decimal(0))
    return total
