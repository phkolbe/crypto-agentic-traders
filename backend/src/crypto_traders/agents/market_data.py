"""Market Data Agent.

Busca candles e ticker das exchanges, normaliza, persiste e publica no bus.

Recebe uma `MarketDataSource` construida **sem credenciais**: dados de mercado
sao publicos, e nao ha motivo para este agente ter poder de gastar dinheiro.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ..bus import EventBus, Topics
from ..config import Settings
from ..db.repositories import AgentRunRepository, CandleRepository
from ..db.session import session_scope
from ..discovery import DEFAULT_EXCLUDED_ASSETS, DiscoveryCriteria, DiscoveryResult, discover
from ..domain.models import Candle
from ..exchanges.base import MarketDataSource, timeframe_seconds
from .base import BaseAgent

#: Teto de tempo para coletar UM par. A rajada e sequencial, entao uma conexao
#: pendurada (TCP aberto sem resposta) para a coleta dos demais pares por tempo
#: indefinido -- sem erro, sem log, sem heartbeat. Medido: com um par pendurado
#: por 3s, o ciclo dos 16 pares levava 3,06s; sem teto, o limite e o do socket.
#:
#: E infraestrutura, nao negocio (D15): nao vem do `.env` nem do banco. O valor
#: cobre com folga as 3 tentativas do retry do adaptador (1s + 2s de backoff mais
#: o timeout do proprio ccxt).
SYMBOL_FETCH_TIMEOUT_SECONDS = 60.0

#: Folga aceita entre o relogio desta maquina e o da exchange ao julgar se um
#: candle ja fechou. Pequena de proposito: e tolerancia de relogio, nao licenca
#: para aceitar candle em formacao.
CLOCK_SKEW_TOLERANCE = timedelta(minutes=2)

#: Fracao do periodo que a tolerancia de relogio nunca pode ultrapassar.
#:
#: `CLOCK_SKEW_TOLERANCE` sozinha e CEGA AO TIMEFRAME: em 1d, 2 minutos sao
#: 0,14% do periodo; em 1m, sao DOIS periodos inteiros -- e timeframe e variavel
#: de negocio editavel pela interface (D15), entao ele muda sem ninguem revisar
#: esta constante. Medido contra a versao anterior: com timeframe 1m, um candle
#: ainda EM FORMACAO era publicado como fechado.
#:
#: A assimetria e deliberada: recusar candle legitimo custa um ciclo (o candle
#: volta, porque a marca de publicado so e gravada quando a publicacao
#: acontece), aceitar candle em formacao custa uma decisao tomada com preco que
#: ainda vai mudar. So a segunda e irreversivel.
CLOCK_SKEW_MAX_FRACTION = 10

#: A partir de quantos periodos sem candle novo o par ganha aviso no log. So
#: registra -- nao muda decisao nenhuma, entao nao e parametro de negocio.
STALE_PERIODS_WARNING = 3


class MarketDataAgent(BaseAgent):
    name = "market_data"

    def __init__(
        self,
        bus: EventBus,
        source: MarketDataSource,
        settings: Settings,
        on_universe_change: Callable[[DiscoveryResult], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(bus)
        self._source = source
        self._settings = settings
        self._last_published: dict[str, datetime] = {}
        """Ultimo `open_time` publicado por simbolo, para nao reprocessar candles.

        A comparacao contra este valor e por MAIOR QUE, nunca por diferente, e a
        entrada so permanece se a publicacao realmente acontecer -- publicacao
        interrompida desfaz a marca, mas SO se a marca ainda for a dela: desfazer
        por cima de uma coleta concorrente que publicou um candle mais novo
        republicaria aquele candle. Ver `_fetch_symbol`.
        """

        self.latest_prices: dict[str, Decimal] = {}
        """Preco corrente do par, consumido pelo Risk e pelo Portfolio.

        A chave e o ativo base ("BTC") para o par cotado em `quote_currency` e o
        nome completo do par para qualquer outra cotacao -- ver
        `_chave_de_preco`, que explica por que uma chave por ativo base sozinha
        misturava dois mercados num numero so.
        """

        self._condicao: dict[str, str] = {}
        """Condicao anormal de coleta vigente por simbolo, para logar a MUDANCA.

        Ver `_log_de_estado`: um par ocioso permanece ocioso por horas, e repetir
        o mesmo aviso a cada ciclo enterra o diagnostico em ruido.
        """

        self._on_universe_change = on_universe_change
        self._discovered: list[str] = []
        self._last_discovery: datetime | None = None
        self.discovery: DiscoveryResult | None = None

    # ------------------------------------------------------------------
    # Universo de pares observados
    # ------------------------------------------------------------------
    @property
    def active_symbols(self) -> list[str]:
        """Pares realmente observados agora.

        Com `SYMBOLS` preenchido e a lista do `.env`; vazio, e o resultado da
        ultima descoberta. Nunca "nenhum": subir sem observar mercado nenhum e o
        estado que mais engana, porque tudo parece saudavel.
        """
        if self._settings.trading.symbols:
            return list(self._settings.trading.symbols)
        return list(self._discovered)

    @property
    def discovery_enabled(self) -> bool:
        return self._settings.trading.discovery_enabled

    def _criteria(self) -> DiscoveryCriteria:
        extra = {asset.upper() for asset in self._settings.trading.discovery_exclude_assets}
        return DiscoveryCriteria(
            quote_currency=self._settings.trading.quote_currency,
            min_quote_volume_24h=self._settings.trading.discovery_min_quote_volume_24h,
            max_symbols=self._settings.trading.discovery_max_symbols,
            exclude_assets=DEFAULT_EXCLUDED_ASSETS | extra,
        )

    async def discover_symbols(self, force: bool = False) -> list[str]:
        """Varre a exchange e atualiza o universo de pares.

        Nao faz nada quando `SYMBOLS` esta preenchido: configuracao explicita
        sempre vence descoberta automatica.
        """
        if not self.discovery_enabled:
            return self.active_symbols

        now = datetime.now(UTC)
        intervalo = timedelta(hours=self._settings.trading.discovery_refresh_hours)
        due = (
            force or self._last_discovery is None or now - self._last_discovery >= intervalo
        )
        if not due:
            return self.active_symbols

        try:
            result = await discover(self._source, self._criteria())
        except Exception as exc:
            # Falha na varredura nao pode derrubar a coleta: seguimos com o
            # universo anterior, que ainda e melhor do que nenhum mercado.
            self.log.error("market_data.discovery_failed", error=str(exc))
            return self.active_symbols

        self._last_discovery = now
        previous = set(self._discovered)
        self._discovered = result.symbols
        self.discovery = result

        if not result.symbols:
            self.log.error(
                "market_data.discovery_empty",
                min_volume=str(self._settings.trading.discovery_min_quote_volume_24h),
                detail="nenhum par atingiu o piso de liquidez; "
                "reveja DISCOVERY_MIN_QUOTE_VOLUME_24H",
            )
        elif set(result.symbols) != previous and self._on_universe_change is not None:
            await self._on_universe_change(result)

        return self.active_symbols

    async def _run(self) -> None:
        while True:
            await self.wait_if_paused()
            await self.discover_symbols()
            await self.refresh()
            await self.heartbeat(detail=f"{len(self.latest_prices)} precos")
            if not await self.sleep(self._settings.trading.market_data_interval_seconds):
                return

    async def refresh(self) -> None:
        """Um ciclo de coleta. Publico para o orquestrador poder aquecer o sistema."""
        for symbol in self.active_symbols:
            try:
                # Teto de tempo por par: sem ele, uma conexao pendurada em um
                # simbolo interrompe a coleta de todos os que vem depois na
                # rajada -- e o pior e que isso nao gera erro nenhum.
                async with asyncio.timeout(SYMBOL_FETCH_TIMEOUT_SECONDS):
                    await self._fetch_symbol(symbol)
            except TimeoutError:
                self._log_de_estado(
                    symbol,
                    "coleta",
                    "timeout",
                    "warning",
                    "market_data.symbol_timeout",
                    limite_segundos=SYMBOL_FETCH_TIMEOUT_SECONDS,
                    detail="a exchange nao respondeu no tempo; seguindo para o proximo par",
                )
            except Exception as exc:
                # Falha em um par nao pode impedir a coleta dos demais: um
                # simbolo deslistado travaria o sistema inteiro.
                #
                # A chave de supressao e o TIPO da excecao, nao o texto dela.
                # Mensagem de erro de exchange carrega id de requisicao e
                # timestamp: o 429 real da Binance via ccxt vem como
                # 'IP banned until 1767225611000', com numero novo a cada
                # tentativa. Chaveando pelo texto, a MESMA falha persistente
                # volta a logar a cada ciclo -- medido: 5 avisos em 5 ciclos --
                # e devolve o ruido de 1.440 linhas por dia por par que enterrou
                # o diagnostico em D25. O texto continua no campo `error` do
                # primeiro aviso; o que nao se repete e a linha.
                self._log_de_estado(
                    symbol,
                    "coleta",
                    f"falha:{type(exc).__name__}",
                    "warning",
                    "market_data.symbol_failed",
                    error=str(exc),
                )

    def _log_de_estado(
        self, symbol: str, familia: str, condicao: str, nivel: str, evento: str, **campos
    ) -> None:
        """Loga uma condicao anormal apenas quando ELA MUDA.

        Par sem negociacao, exchange com relogio torto ou serie interrompida sao
        estados que duram horas. Com cadencia de 60s e 16 pares, repetir o aviso
        a cada ciclo daria 1.440 linhas por dia POR PAR -- foi exatamente esse
        ruido que enterrou o diagnostico em D25 (17 reinicios em 16 minutos,
        cada um com seu alerta). Mudanca de estado e informacao; repeticao nao e.

        `familia` separa "nao consegui coletar" de "coletei e tinha candle
        estranho": as duas coisas podem acontecer no MESMO ciclo (a serie veio,
        mas com um candle do futuro no fim), e uma nao pode zerar o aviso da
        outra -- foi assim que o aviso de relogio torto voltou a sair 5 vezes em
        5 ciclos na medicao.
        """
        chave = f"{symbol}|{familia}"
        if self._condicao.get(chave) == condicao:
            return
        self._condicao[chave] = condicao
        getattr(self.log, nivel)(evento, symbol=symbol, **campos)

    def _log_de_normalidade(self, symbol: str) -> None:
        """Fecha o ciclo do aviso: coleta anormal que voltou ao normal se diz uma vez."""
        if self._condicao.pop(f"{symbol}|coleta", None) is not None:
            self.log.info("market_data.coleta_normalizada", symbol=symbol)

    def _esquece_anomalia(self, symbol: str) -> None:
        """Ciclo inteiro sem candle recusado: o proximo problema volta a avisar.

        Silencioso de proposito -- "nada de errado" nao e novidade digna de log.
        """
        self._condicao.pop(f"{symbol}|candle", None)

    def _closed_confiaveis(self, symbol: str, candles: list[Candle]) -> list[Candle]:
        """Candles que podemos tratar como fato consumado.

        Nao confiamos apenas no campo `closed` (que o adaptador deduz por
        posicao na resposta) nem no relogio da exchange. Um candle so conta se
        for do par, do periodo e da EXCHANGE pedidos, se o periodo dele JA
        TERMINOU pelo nosso relogio e se os precos sao internamente coerentes:
        nenhum <= 0, `high >= low`, e `open` e `close` DENTRO de [low, high] --
        que e o que "high" e "low" significam.

        O filtro roda ANTES da gravacao de proposito: candle do futuro ou com
        preco zero gravado no banco envenena `history()`, que e a janela que a
        estrategia usa para calcular indicadores -- o dano existiria mesmo sem
        publicacao nenhuma.
        """
        timeframe = self._settings.trading.timeframe
        try:
            duracao = timedelta(seconds=timeframe_seconds(timeframe))
        except ValueError as exc:
            # Sem saber quanto dura um candle nao ha como saber se ele fechou.
            # Diante da duvida o sistema nao opera -- e diz por que, em ALTO,
            # porque parar em silencio e o modo de falha que mais engana.
            self._log_de_estado(
                symbol,
                "candle",
                f"timeframe_invalido:{timeframe}",
                "error",
                "market_data.timeframe_invalido",
                timeframe=timeframe,
                error=str(exc),
                detail="sem duracao de candle nao se decide nada; nenhuma coleta publicada",
            )
            return []

        agora = datetime.now(UTC)
        # A tolerancia nunca pode chegar perto de um periodo inteiro: ver
        # CLOCK_SKEW_MAX_FRACTION.
        limite = agora + min(CLOCK_SKEW_TOLERANCE, duracao / CLOCK_SKEW_MAX_FRACTION)
        exchange_pedida = str(self._settings.exchange).lower()
        confiaveis: list[Candle] = []
        recusados = 0
        for candle in candles:
            if not candle.closed:
                continue
            if (
                candle.symbol != symbol
                or candle.timeframe != timeframe
                or str(candle.exchange).lower() != exchange_pedida
            ):
                # Candle que nao e do par, do periodo nem da EXCHANGE pedidos.
                # Tres coisas dependem dessa identidade e nenhuma delas a confere
                # sozinha:
                # `latest_prices` e indexado pelo simbolo pedido -- e e o preco
                # que o Risk e o Portfolio usam para avaliar posicao e disparar
                # stop --, o julgamento "este periodo fechou?" usa a duracao do
                # timeframe pedido, e `history()` le a janela por
                # `settings.exchange`. Medido na versao anterior: um candle
                # DOGE/USDT devolvido na resposta de BTC/USDT virava
                # `latest_prices["BTC"] = 0.4`, e um candle de 1m entregue com o
                # sistema em 1d era gravado como candle diario, envenenando a
                # janela que `history()` entrega a estrategia.
                #
                # A exchange entrou na conferencia porque ela partia a coleta em
                # duas metades incoerentes: candle carimbado com outra exchange
                # era publicado no bus e definia `latest_prices`, mas era gravado
                # numa linha que `history()` NUNCA le -- evento com preco de um
                # mercado e janela historica vazia no outro, o pior dos dois
                # mundos. Recusar e a unica resposta coerente: preco e janela tem
                # de vir do MESMO livro de ofertas.
                #
                # O adaptador ccxt carimba os tres campos com o que foi pedido
                # (ccxt_adapter.py), entao hoje isso nao vem da Binance: e defesa
                # em profundidade no unico ponto onde preco de mercado entra no
                # sistema, contra proxy, cache trocado e replica com failover.
                self._log_de_estado(
                    symbol,
                    "candle",
                    f"identidade:{candle.exchange}:{candle.symbol}:{candle.timeframe}",
                    "error",
                    "market_data.candle_de_outra_identidade",
                    open_time=candle.open_time.isoformat(),
                    symbol_recebido=candle.symbol,
                    timeframe_recebido=candle.timeframe,
                    timeframe_pedido=timeframe,
                    exchange_recebida=str(candle.exchange),
                    exchange_pedida=str(self._settings.exchange),
                    detail="resposta nao corresponde ao par/periodo/exchange pedidos; descartada",
                )
                recusados += 1
                continue
            if min(candle.open, candle.high, candle.low, candle.close) <= 0:
                # Preco zero ou negativo vira avaliacao zero de posicao no
                # Portfolio e no Risk. Nao existe candle real assim.
                self._log_de_estado(
                    symbol,
                    "candle",
                    f"sem_preco:{candle.open_time.isoformat()}",
                    "warning",
                    "market_data.candle_sem_preco",
                    open_time=candle.open_time.isoformat(),
                    close=str(candle.close),
                )
                recusados += 1
                continue
            if candle.high < candle.low:
                self._log_de_estado(
                    symbol,
                    "candle",
                    f"incoerente:{candle.open_time.isoformat()}",
                    "warning",
                    "market_data.candle_incoerente",
                    open_time=candle.open_time.isoformat(),
                    detail=f"high {candle.high} menor que low {candle.low}",
                )
                recusados += 1
                continue
            fora_da_faixa = [
                f"{nome} {valor}"
                for nome, valor in (("open", candle.open), ("close", candle.close))
                if valor < candle.low or valor > candle.high
            ]
            if fora_da_faixa:
                # `high` e `low` sao, por definicao, o maximo e o minimo do
                # periodo: `open` e `close` estao DENTRO deles ou o candle nao
                # descreve nenhum mercado real. Um candle low=99, high=101 e
                # close=1.000.000 e internamente impossivel, e passava por
                # fechado e confiavel porque as conferencias anteriores olham
                # `high < low` e preco <= 0, nunca a faixa.
                #
                # Isso importa mais que estetica de dado: `close` e o UNICO campo
                # que viaja para `latest_prices`, o preco com que o Portfolio
                # avalia a posicao e o Risk decide stop e take profit. Um close
                # inflado dispara take profit numa posicao que nao subiu; um
                # close esmagado dispara stop numa que nao caiu -- e as duas
                # coisas viram ordem a mercado com dinheiro real.
                #
                # `open` entra na mesma recusa porque o candle inteiro e gravado
                # e alimenta os indicadores que `history()` entrega a estrategia:
                # a faixa quebrada em qualquer ponta e a mesma prova de que a
                # linha nao veio inteira do livro de ofertas.
                self._log_de_estado(
                    symbol,
                    "candle",
                    f"fora_da_faixa:{candle.open_time.isoformat()}",
                    "warning",
                    "market_data.candle_fora_da_faixa",
                    open_time=candle.open_time.isoformat(),
                    high=str(candle.high),
                    low=str(candle.low),
                    detail=(
                        f"{', '.join(fora_da_faixa)} fora de "
                        f"[{candle.low}, {candle.high}]; candle impossivel, descartado"
                    ),
                )
                recusados += 1
                continue
            if candle.open_time + duracao > limite:
                # Relogio da exchange adiantado, ou marcacao de fechado errada.
                # Aceitar seria duplamente ruim: decidiriamos com preco que ainda
                # vai mudar E o simbolo travaria, porque nenhum candle real
                # seria mais novo que um candle do futuro.
                self._log_de_estado(
                    symbol,
                    "candle",
                    f"futuro:{candle.open_time.isoformat()}",
                    "error",
                    "market_data.candle_no_futuro",
                    open_time=candle.open_time.isoformat(),
                    fecha_em=(candle.open_time + duracao).isoformat(),
                    agora=agora.isoformat(),
                    detail="candle marcado como fechado antes do periodo terminar; descartado",
                )
                recusados += 1
                continue
            confiaveis.append(candle)

        if not recusados:
            self._esquece_anomalia(symbol)
        if not confiaveis:
            self._log_de_estado(
                symbol,
                "coleta",
                "sem_candle_fechado",
                "warning",
                "market_data.sem_candle_fechado",
                detail="a exchange nao devolveu nenhum candle fechado confiavel neste ciclo",
            )
        return confiaveis

    def _chave_de_preco(self, symbol: str) -> str:
        """Chave de `latest_prices` para um par, sem misturar moedas de cotacao.

        `symbol.partition("/")[0]` -- so o ativo base -- era conveniente e
        errado: BTC/USDC e BTC/USDT gravavam na MESMA chave "BTC" e o ultimo par
        da rajada vencia, em silencio. Nao e cenario teorico: `SYMBOLS` e
        variavel de negocio editavel pela interface (D15) e o sistema acabou de
        migrar de BRL para USDC (D24), entao um par da cotacao antiga esquecido
        na lista basta para o preco de um mercado avaliar a posicao do outro.

        Por que o ativo base continua sendo a chave do par principal: o Portfolio
        e o Risk leem este dicionario por ativo (`prices.get(position.asset)`),
        multiplicam a quantidade em carteira pelo valor encontrado e somam ao
        caixa, que esta em `quote_currency`. A chave por ativo base, portanto, so
        significa algo para o par cotado NA MOEDA DE COTACAO do sistema -- para
        os outros, o numero esta em outra unidade e somar seria erro de conta,
        nao arredondamento (imagine um par cotado em BTC).

        Entao o par divergente guarda o preco sob o nome COMPLETO. Ele nao se
        perde (continua publicado, gravado e visivel no dashboard) e nao pode ser
        confundido com o preco em `quote_currency`: quem le por ativo simplesmente
        nao o encontra, e o Portfolio ja trata preco ausente como ausente, o
        caminho conservador que ele chama de `unpriced`.
        """
        base, _, quote = symbol.partition("/")
        if quote.upper() == self._settings.trading.quote_currency.upper():
            return base
        self._log_de_estado(
            symbol,
            "cotacao",
            f"divergente:{quote}",
            "warning",
            "market_data.cotacao_divergente",
            quote_recebida=quote,
            quote_currency=self._settings.trading.quote_currency,
            chave=symbol,
            detail=(
                "par cotado em outra moeda que a do sistema; preco guardado sob o "
                "nome completo do par para nao se somar ao patrimonio como se "
                "estivesse na moeda de cotacao"
            ),
        )
        return symbol

    async def _fetch_symbol(self, symbol: str) -> None:
        candles = await self._source.fetch_candles(
            symbol, self._settings.trading.timeframe, self._settings.trading.candle_history_limit
        )
        if not candles:
            self._log_de_estado(
                symbol,
                "coleta",
                "resposta_vazia",
                "warning",
                "market_data.resposta_vazia",
                detail="a exchange devolveu zero candles; par deslistado ou sem negociacao?",
            )
            return

        closed = self._closed_confiaveis(symbol, candles)
        if not closed:
            return
        self._log_de_normalidade(symbol)

        async with session_scope(self._settings) as session:
            stored = await CandleRepository(session).upsert_many(closed)
            await AgentRunRepository(session).heartbeat(
                self.name, str(self.state), f"{symbol}: {stored} candles novos"
            )

        # `max` e nao `[-1]`: a lista pode chegar fora de ordem, e "o ultimo da
        # lista" nao e necessariamente o mais recente no tempo.
        latest = max(closed, key=lambda candle: candle.open_time)
        anterior = self._last_published.get(symbol)

        # Publicamos apenas quando o candle fechado e MAIS NOVO que o ultimo
        # publicado. Comparar por diferente (`!=`) parece equivalente e nao e: a
        # exchange que alterna entre dois candles (replica atrasada, cache,
        # failover) republica os dois em ciclo -- A, B, A, B -- e cada
        # publicacao faz a estrategia reavaliar e, com cooldown vencido,
        # reemitir o mesmo sinal indefinidamente. Medido: 6 publicacoes para 2
        # candles em 6 ciclos.
        if anterior is not None and latest.open_time <= anterior:
            if latest.open_time < anterior:
                self._log_de_estado(
                    symbol,
                    "candle",
                    f"fora_de_ordem:{latest.open_time.isoformat()}",
                    "warning",
                    "market_data.candle_fora_de_ordem",
                    open_time=latest.open_time.isoformat(),
                    ultimo_publicado=anterior.isoformat(),
                    detail="candle anterior ao ultimo publicado; nao republicado",
                )
            return

        # O preco corrente so anda para frente junto com o candle aceito: um
        # candle atrasado nao pode reescrever o preco que o Risk e o Portfolio
        # usam para avaliar posicao.
        self.latest_prices[self._chave_de_preco(symbol)] = latest.close

        duracao = timedelta(seconds=timeframe_seconds(self._settings.trading.timeframe))
        atraso = datetime.now(UTC) - (latest.open_time + duracao)
        if atraso > STALE_PERIODS_WARNING * duracao:
            # Par sem negociacao no periodo, ou serie interrompida na exchange.
            # E aviso, nao recusa: o candle e legitimo, so e antigo.
            self.log.warning(
                "market_data.serie_estagnada",
                symbol=symbol,
                open_time=latest.open_time.isoformat(),
                atraso_horas=round(atraso.total_seconds() / 3600, 1),
            )

        # Reservar a publicacao e depois DESFAZER a reserva se ela nao acontecer.
        # A ordem destas linhas importa nos dois sentidos, e por motivos opostos:
        #
        # - marcar ANTES de publicar e o que impede publicacao DUPLA. Nao existe
        #   `await` nenhum entre a leitura de `_last_published` (`anterior`,
        #   acima) e esta escrita, entao duas coletas concorrentes do mesmo par
        #   -- exatamente o que o orquestrador faz na subida, ao criar a tarefa
        #   do laco e chamar `refresh()` de novo para aquecer o portfolio -- nao
        #   podem ver as duas "nunca publiquei". NAO INSIRA `await` entre
        #   aquela leitura e esta linha.
        # - desfazer a marca quando a publicacao falha e o que impede publicacao
        #   PERDIDA. A comparacao do filtro e por "mais novo que", entao candle
        #   marcado sem ter sido publicado nao volta NUNCA: em 1d, o sinal
        #   daquele par se perde por um dia inteiro, em silencio. Tres
        #   interrupcoes reais entre a marca e a publicacao, todas medidas:
        #   o cancelamento da tarefa no `restart()` do watchdog (o achado 2 de
        #   D25), o teto de SYMBOL_FETCH_TIMEOUT_SECONDS por par, e a excecao do
        #   bus de Redis quando a conexao cai (`xadd` awaita rede).
        #
        # O resultado e at-least-once, que e a semantica que o proprio filtro de
        # monotonicidade acima sabe absorver -- o mesmo tratamento que a falha de
        # gravacao no banco ja tinha.
        self._last_published[symbol] = latest.open_time
        try:
            await self.bus.publish(Topics.CANDLES, latest)
        except BaseException:
            # `BaseException` de proposito: `CancelledError` nao herda de
            # `Exception`, e o cancelamento e justamente o caminho que D25
            # mostrou acontecer de verdade, 17 vezes em 16 minutos.
            #
            # O rollback so vale se a marca ainda for A NOSSA. Entre a nossa
            # escrita e este `except` houve um `await` (a publicacao), e e
            # justamente ai que outra coleta do mesmo par -- o que o orquestrador
            # faz na subida -- pode ter publicado um candle MAIS NOVO com
            # sucesso e marcado a dela. Restaurar `anterior` por cima daquela
            # marca apagaria uma publicacao que DEU CERTO, e o candle ja
            # entregue voltaria no ciclo seguinte: a republicacao que faz a
            # estrategia reemitir o mesmo sinal, exatamente o que o filtro de
            # monotonicidade existe para evitar. Desfazer o que nao e nosso
            # trocaria publicacao perdida por publicacao dobrada -- e a dobrada
            # e a que vira ordem.
            if self._last_published.get(symbol) != latest.open_time:
                self.log.warning(
                    "market_data.publicacao_desfeita_ignorada",
                    symbol=symbol,
                    open_time=latest.open_time.isoformat(),
                    marca_vigente=str(self._last_published.get(symbol)),
                    detail=(
                        "publicacao interrompida, mas outra coleta ja publicou candle "
                        "mais novo; marca preservada para nao republicar"
                    ),
                )
                raise
            # Volta ao valor ANTERIOR, e nao ao vazio: apagar a marca
            # republicaria o candle velho no ciclo seguinte, que e exatamente o
            # sinal reemitido que o filtro acima existe para evitar.
            if anterior is None:
                self._last_published.pop(symbol, None)
            else:
                self._last_published[symbol] = anterior
            # `latest_prices` NAO e desfeito, de proposito: aquele preco passou
            # por todos os filtros, e preco mais fresco nunca e pior para o Risk
            # avaliar posicao. Quem depende do EVENTO e reatendido no proximo
            # ciclo, pela marca que acabou de ser devolvida.
            # Candle sumindo em silencio foi o modo de falha; esta linha e o
            # antidoto, e ela sai ANTES de a excecao subir.
            self.log.warning(
                "market_data.publicacao_desfeita",
                symbol=symbol,
                open_time=latest.open_time.isoformat(),
                detail="publicacao interrompida; candle liberado para reenvio no proximo ciclo",
            )
            raise
        self.log.info(
            "market_data.candle_published",
            symbol=symbol,
            open_time=latest.open_time.isoformat(),
            close=str(latest.close),
        )

    async def history(self, symbol: str, limit: int | None = None) -> list[Candle]:
        """Janela historica do banco, usada pelo Strategy Agent e pelo backtest."""
        async with session_scope(self._settings) as session:
            return await CandleRepository(session).recent(
                self._settings.exchange,
                symbol,
                self._settings.trading.timeframe,
                limit or self._settings.trading.candle_history_limit,
            )

    async def on_stop(self) -> None:
        await self._source.close()
