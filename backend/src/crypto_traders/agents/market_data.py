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
from ..db.repositories import (
    AgentRunRepository,
    CandleRepository,
    PortfolioSnapshotRepository,
)
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

#: Teto de tempo para ler o preco corrente (ticker) de TODOS os pares numa chamada.
TICKER_FETCH_TIMEOUT_SECONDS = 30.0

#: Teto por par quando o lote falha e a leitura cai para um par de cada vez.
TICKER_PAR_TIMEOUT_SECONDS = 10.0

#: Idade maxima do preco corrente para ele valer na avaliacao de stop e alvo,
#: em ciclos de coleta (com piso de 3 minutos). Mais velho que isso, o preco ja
#: nao e "corrente": stop e alvo do par ficam SEM avaliacao (com alerta) ate ele
#: voltar. Nunca se compara stop com o fechamento do candle -- o preco medio veio
#: de uma execucao no preco de mercado, e o fechamento diario pode estar abaixo
#: do stop com o mercado acima dele (venda fantasma, medida pelo critico).
TICKER_MAX_AGE_CYCLES = 3
TICKER_MAX_AGE_MIN = timedelta(minutes=3)

#: Salto, contra o ultimo preco valido, a partir do qual UMA leitura nao basta
#: (fator multiplicativo, nos dois sentidos). Fica guardada como suspeita e so
#: vale se a leitura seguinte confirmar o mesmo lado. Assim um numero corrompido
#: (decimal trocado, preco de outro par) nao dispara stop, e um colapso de
#: verdade -- que persiste -- e aceito um ciclo depois, em vez de ser mascarado.
TICKER_SALTO_SUSPEITO = Decimal(3)


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

        self.precos_correntes: dict[str, Decimal] = {}
        """Preco CORRENTE (ticker) do par, lido a cada ciclo -- mesma chave de
        `latest_prices`.

        `latest_prices` e o fechamento do ultimo candle FECHADO do timeframe da
        estrategia: com `1d`, ele anda uma vez por dia. Era esse o preco com que o
        Risk Manager comparava stop e alvo "a cada 60s" -- e com que o broker de
        papel executava. Medido no dry_run (04/11/2026): o stop de 3% so podia agir
        no fechamento diario, e o papel comprou o XRP a 1,2957 (fechamento da
        vespera) com o mercado a 1,3962, gerando um "take profit" de +7,7% em 38 s
        que nao existiu. O candle continua sendo o que a ESTRATEGIA le; quem avalia
        posicao e executa le `precos_para_avaliacao`.
        """

        self._precos_correntes_em: dict[str, datetime] = {}
        self.pares_sem_preco_corrente: list[str] = []
        """Pares vigiados (universo e carteira) sem preco corrente recente: stop e
        alvo deles nao sao avaliados."""

        self._suspeitos: dict[str, Decimal] = {}
        """Leitura com salto grande, a espera da confirmacao do ciclo seguinte."""

        self._pendentes: dict[str, tuple[str, Decimal, datetime]] = {}
        """Ticker sem referencia nenhuma ainda (primeira leitura do processo): julgado
        contra o fechamento do candle quando os candles do ciclo chegarem."""

        self._alertados_sem_preco: frozenset[str] = frozenset()

        self._pares_em_carteira: dict[str, Decimal] = {}
        """Par -> preco do ultimo retrato, para cada posicao com preco medio.

        O universo (`active_symbols`) define o que se pode ABRIR, nao o que se
        protege. Antes, um ativo em carteira cujo par saia da lista -- revarredura
        de 24h, ou o operador editando SYMBOLS -- deixava de ter ticker; em 3 min
        sumia de `precos_de_mercado`, o Risk pulava o stop, e o aviso de frescor,
        calculado so sobre os pares ativos, nao dizia nada (auditoria 09/11/2026,
        P0 #7). So posicao com preco medio entra: sem ele nao ha stop, e pedir
        ticker de um par que talvez nem exista so arriscaria a leitura.
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
        """Um ciclo de coleta. Publico para o orquestrador poder aquecer o sistema.

        O preco corrente vem ANTES dos candles: o candle publicado aqui dispara
        estrategia -> risco -> execucao, e a ordem que sai desse caminho tem de
        encontrar o preco deste ciclo, nao o do anterior.
        """
        await self._atualizar_precos_correntes()
        await self._coletar_candles()
        self._julgar_pendentes()
        await self._avisar_frescor()

    async def _coletar_candles(self) -> None:
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

    # ------------------------------------------------------------------
    # Preco corrente
    # ------------------------------------------------------------------
    def idade_maxima_do_preco(self) -> timedelta:
        ciclo = timedelta(seconds=self._settings.trading.market_data_interval_seconds)
        return max(TICKER_MAX_AGE_MIN, ciclo * TICKER_MAX_AGE_CYCLES)

    async def _atualizar_precos_correntes(self) -> None:
        """Le o ticker dos pares ativos e dos pares em carteira fora deles.

        Falha aqui nao interrompe o ciclo: os candles ainda chegam. O par sem
        preco corrente fica sem avaliacao de stop e alvo (com alerta) ate voltar.

        Os pares em carteira fora do universo sao lidos par a par, fora do lote:
        um deles deslistado (`BadSymbol`) derrubaria a leitura em lote de todos os
        outros a cada ciclo.
        """
        await self._ler_carteira()
        symbols = self.active_symbols
        if symbols:
            await self._ler_tickers(symbols)
        fora = [s for s in self._pares_em_carteira if s not in symbols]
        if fora:
            await self._ler_tickers_par_a_par(fora)

    async def _ler_carteira(self) -> None:
        """Pares das posicoes com preco medio no ultimo retrato gravado deste modo.

        Pelo banco, e nao por uma assinatura do topico de retratos: e o mesmo
        retrato que o Portfolio grava, nao depende de uma tarefa a mais viva neste
        agente, e falhar aqui so mantem a lista anterior.
        """
        quote = self._settings.trading.quote_currency
        try:
            async with session_scope(self._settings) as session:
                retrato = await PortfolioSnapshotRepository(session).latest(
                    mode=str(self._settings.trading_mode)
                )
        except Exception as exc:
            self._log_de_estado(
                "*",
                "carteira",
                f"falha:{type(exc).__name__}",
                "warning",
                "market_data.carteira_ilegivel",
                error=str(exc),
                detail="pares em carteira fora do universo mantidos da leitura anterior",
            )
            return
        self._condicao.pop("*|carteira", None)
        pares: dict[str, Decimal] = {}
        for posicao in (retrato.positions or []) if retrato is not None else []:
            asset = posicao.get("asset")
            if not asset or asset.upper() == quote.upper():
                continue
            try:
                quantidade = Decimal(str(posicao.get("quantity") or 0))
                medio = Decimal(str(posicao.get("average_price") or 0))
                preco = Decimal(str(posicao.get("current_price") or 0))
            except ArithmeticError:
                continue
            if quantidade > 0 and medio > 0:
                pares[f"{asset}/{quote}"] = preco
        self._pares_em_carteira = pares

    async def _ler_tickers(self, symbols: list[str]) -> None:
        varios = getattr(self._source, "fetch_tickers", None)
        if varios is not None:
            try:
                async with asyncio.timeout(TICKER_FETCH_TIMEOUT_SECONDS):
                    tickers = await varios(symbols)
            except Exception as exc:
                # Um simbolo desconhecido (typo, par deslistado) derruba o lote
                # inteiro no ccxt (`BadSymbol`). Sem a leitura par a par abaixo,
                # um par ruim cegava o stop de TODOS, ciclo apos ciclo.
                self._log_de_estado(
                    "*",
                    "preco_corrente",
                    f"falha:{type(exc).__name__}",
                    "warning",
                    "market_data.ticker_lote_falhou",
                    error=str(exc),
                    detail="lendo o preco corrente par a par neste ciclo",
                )
            else:
                self._condicao.pop("*|preco_corrente", None)
                agora = datetime.now(UTC)
                for symbol, ticker in tickers.items():
                    self._receber_ticker(symbol, ticker.price, agora)
                return
        await self._ler_tickers_par_a_par(symbols)

    async def _ler_tickers_par_a_par(self, symbols: list[str]) -> None:
        for symbol in symbols:
            try:
                async with asyncio.timeout(TICKER_PAR_TIMEOUT_SECONDS):
                    ticker = await self._source.fetch_ticker(symbol)
            except Exception as exc:
                self._log_de_estado(
                    symbol,
                    "ticker_par",
                    f"falha:{type(exc).__name__}",
                    "warning",
                    "market_data.ticker_falhou",
                    error=str(exc),
                )
                continue
            self._condicao.pop(f"{symbol}|ticker_par", None)
            self._receber_ticker(symbol, ticker.price, datetime.now(UTC))

    def _receber_ticker(self, symbol: str, preco: Decimal, agora: datetime) -> None:
        chave = self._chave_de_preco(symbol)
        if not preco.is_finite() or preco <= 0:
            self._log_de_estado(
                symbol,
                "ticker",
                f"recusado:{preco}",
                "warning",
                "market_data.ticker_recusado",
                preco=str(preco),
                detail="preco corrente invalido; ignorado",
            )
            return
        # Par em carteira fora do universo nao tem candle coletado: a referencia
        # do salto, na primeira leitura, e o preco com que o retrato o avaliou.
        referencia = (
            self.precos_correntes.get(chave)
            or self.latest_prices.get(chave)
            or self._pares_em_carteira.get(symbol)
        )
        if referencia is None or referencia <= 0:
            self._pendentes[chave] = (symbol, preco, agora)
            return
        self._julgar(chave, symbol, preco, referencia, agora)

    def _julgar(
        self, chave: str, symbol: str, preco: Decimal, referencia: Decimal | None, agora: datetime
    ) -> None:
        """Aceita o preco, ou o guarda como suspeito ate a proxima leitura confirmar."""
        dentro = referencia is not None and (
            referencia / TICKER_SALTO_SUSPEITO <= preco <= referencia * TICKER_SALTO_SUSPEITO
        )
        if not dentro:
            anterior = self._suspeitos.get(chave)
            mesmo_lado = (
                anterior is not None
                and referencia is not None
                and (anterior > referencia) == (preco > referencia)
            ) or (anterior is not None and referencia is None)
            if not mesmo_lado:
                self._suspeitos[chave] = preco
                self._log_de_estado(
                    symbol,
                    "ticker",
                    f"suspeito:{preco}",
                    "warning",
                    "market_data.ticker_suspeito",
                    preco=str(preco),
                    referencia=str(referencia),
                    detail="salto grande demais para uma leitura so; vale se a proxima confirmar",
                )
                return
            self.log.warning(
                "market_data.salto_confirmado",
                symbol=symbol,
                preco=str(preco),
                referencia=str(referencia),
                detail="duas leituras seguidas confirmam o movimento extremo",
            )
        self._suspeitos.pop(chave, None)
        self._condicao.pop(f"{symbol}|ticker", None)
        self.precos_correntes[chave] = preco
        self._precos_correntes_em[chave] = agora

    def _julgar_pendentes(self) -> None:
        """Primeira leitura do processo: julgada contra o fechamento recem-coletado."""
        pendentes, self._pendentes = self._pendentes, {}
        for chave, (symbol, preco, em) in pendentes.items():
            self._julgar(chave, symbol, preco, self.latest_prices.get(chave), em)

    def _chaves_ativas(self) -> set[str]:
        """Pares vigiados: o universo e as posicoes em carteira fora dele."""
        return {self._chave_de_preco(s) for s in self.active_symbols} | {
            self._chave_de_preco(s) for s in self._pares_em_carteira
        }

    def precos_de_mercado(self) -> dict[str, Decimal]:
        """So o preco corrente RECENTE (`idade_maxima_do_preco`), por ativo.

        E com este que o Risk Manager compara stop e alvo e dimensiona a ordem.
        Par ausente aqui fica sem avaliacao de protecao -- nunca com o fechamento
        do candle, que nao e o mercado.
        """
        agora = datetime.now(UTC)
        limite = self.idade_maxima_do_preco()
        return {
            chave: preco
            for chave, preco in self.precos_correntes.items()
            if agora - self._precos_correntes_em[chave] <= limite
        }

    def precos_para_avaliacao(self) -> dict[str, Decimal]:
        """O melhor preco conhecido de cada ativo: valorizacao da carteira e papel.

        O ultimo preco corrente aceito (mesmo velho: foi o mercado de verdade);
        sem nenhum, o fechamento do candle. Serve para dizer quanto a carteira vale
        e a que preco o broker de papel executa -- nao para disparar stop
        (`precos_de_mercado`). Nunca inventa preco: par sem nada fica ausente.
        """
        precos = dict(self.latest_prices)
        precos.update(self.precos_correntes)
        frescos = self.precos_de_mercado()
        self.pares_sem_preco_corrente = sorted(self._chaves_ativas() - set(frescos))
        return precos

    async def _avisar_frescor(self) -> None:
        """Alerta (nao so log) quando pares ativos ficam sem preco corrente."""
        sem = frozenset(self._chaves_ativas() - set(self.precos_de_mercado()))
        self.pares_sem_preco_corrente = sorted(sem)
        anteriores = self._alertados_sem_preco
        novos = sorted(sem - anteriores)
        try:
            await self._publicar_frescor(sem, anteriores, novos)
        except Exception as exc:
            # O alerta nunca derruba a coleta; sem registrar o estado, ele e
            # tentado de novo no proximo ciclo.
            self.log.error("market_data.alerta_de_preco_falhou", error=str(exc))
            return
        self._alertados_sem_preco = sem

    async def _publicar_frescor(
        self, sem: frozenset[str], anteriores: frozenset[str], novos: list[str]
    ) -> None:
        if novos:
            self.log.warning(
                "market_data.sem_preco_corrente",
                pares=novos,
                detail="stop e alvo destes pares nao sao avaliados ate o preco voltar",
            )
            await self.bus.publish(
                Topics.ALERTS,
                {
                    "type": "preco_corrente_ausente",
                    "title": f"Sem preco corrente: {', '.join(novos)}",
                    "message": (
                        "A exchange nao entregou preco corrente valido para "
                        f"{', '.join(novos)} nos ultimos {self.idade_maxima_do_preco()}. "
                        "Enquanto isso, stop-loss e take-profit destes ativos NAO estao "
                        "sendo avaliados (comparar com o fechamento do candle venderia "
                        "por engano)."
                    ),
                },
            )
        elif anteriores and not sem:
            self.log.info("market_data.preco_corrente_restabelecido", pares=sorted(anteriores))
            await self.bus.publish(
                Topics.ALERTS,
                {
                    "type": "preco_corrente_restabelecido",
                    "title": "Preco corrente restabelecido",
                    "message": (
                        f"{', '.join(sorted(anteriores))}: stop e alvo voltaram a ser avaliados."
                    ),
                },
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
