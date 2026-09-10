"""Isolamento da suite: banco do ensaio, exchange real e diretorio de trabalho.

Este arquivo nao mede regra de negocio nenhuma. Mede o que a suite pode
DANIFICAR enquanto roda, e o que ela deixa de medir por nao ter servidor.

Tres perimetros, e a razao de cada um:

* **`data/crypto_traders.db`** e o banco do ensaio em dry_run. E dele que sai o
  preco medio de cada posicao, e o preco medio e a UNICA entrada do nivel de
  stop-loss. Uma linha de teste gravada ali envenena o medio do dinheiro real
  exatamente como o trade de dry_run envenenava (defeito 2 do gauntlet) -- com a
  diferenca de que `trades` tem gatilho append-only, entao a linha errada NAO
  sai depois. O `conftest` tem uma barreira; aqui ela e PROVADA disparando, e
  nao conferida por leitura.
* **A exchange.** Nenhum teste pode enviar ordem para fora da maquina. A prova
  aqui e sobre o que o codigo faz quando a exchange responde mal -- timeout,
  ordem recusada, saldo insuficiente -- com dublê, e com o que ficou sem medicao
  escrito no proprio teste.
* **O diretorio de trabalho.** Teste que muda de resultado conforme o `cd` de
  quem roda o pytest nao mede o sistema; mede o shell.
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import ccxt.async_support as ccxt
import pytest
from conftest import BANCO_DO_ENSAIO, BancoDoEnsaioTocado, _e_o_banco_do_ensaio
from helpers import RAIZ_DO_REPO, arquivo_do_repo

from crypto_traders.config import DEFAULT_DB_PATH, ExchangeCredentials, Settings
from crypto_traders.db.repositories import TradeRepository
from crypto_traders.db.session import get_engine, session_scope
from crypto_traders.domain.enums import ExchangeName, OrderStatus, OrderType, Side
from crypto_traders.domain.models import OrderRequest
from crypto_traders.exchanges.base import ExchangeError
from crypto_traders.exchanges.ccxt_adapter import CcxtExchange

# ---------------------------------------------------------------------------
# 1. O banco do ensaio
# ---------------------------------------------------------------------------


class TestOBancoDoEnsaioNaoPodeSerTocado:
    """A barreira do `conftest`, vista disparando.

    "O conftest usa tmp_path" e leitura de codigo, e este projeto ja descobriu
    cinco defeitos graves em codigo que parecia correto lido. O que segue prova
    que a protecao AGE.
    """

    def test_o_arquivo_do_ensaio_e_o_que_a_producao_usaria(self):
        """Sem isto, a barreira poderia estar guardando um caminho que ninguem usa."""
        assert BANCO_DO_ENSAIO == DEFAULT_DB_PATH
        assert BANCO_DO_ENSAIO.name == "crypto_traders.db"
        assert BANCO_DO_ENSAIO.parent.name == "data"

    def test_um_settings_sem_url_ainda_aponta_para_o_ensaio(self):
        """O PERIGO existe, e e por isso que a barreira nao e decorativa.

        `Settings` sem `database_url` resolve para o banco do ensaio -- e a suite
        constroi esse objeto em uma duzia de lugares (`Settings(_env_file=None)`
        em test_critico_item4, test_execution, test_strategies...). Nenhum deles
        abre conexao hoje, e nada garantia isso: e uma linha de distancia.

        Este teste falharia se alguem "consertasse" o perigo redirecionando o
        padrao -- e ai a barreira poderia sair. Enquanto ele passa, ela fica.
        """
        configurado = Settings(_env_file=None)
        assert _e_o_banco_do_ensaio(configurado.database_url), (
            f"database_url resolvido = {configurado.database_url}"
        )

    async def test_a_barreira_recusa_o_engine_sobre_o_banco_do_ensaio(self):
        """O caminho da aplicacao: `get_engine` -> `create_async_engine`."""
        perigoso = Settings(
            _env_file=None,
            database_url=f"sqlite+aiosqlite:///{BANCO_DO_ENSAIO.as_posix()}",
        )
        with pytest.raises(BancoDoEnsaioTocado):
            get_engine(perigoso)

    async def test_a_barreira_recusa_o_settings_de_fabrica(self):
        """E o objeto que um teste distraido construiria, sem `database_url`."""
        with pytest.raises(BancoDoEnsaioTocado):
            get_engine(Settings(_env_file=None))

    def test_a_barreira_recusa_sqlite3_cru(self):
        """A outra porta: a suite conecta crua para conferir gatilhos."""
        with pytest.raises(BancoDoEnsaioTocado):
            sqlite3.connect(str(BANCO_DO_ENSAIO))

    def test_a_barreira_recusa_o_caminho_relativo_para_o_mesmo_arquivo(self):
        """Nao depende de grafia: compara arquivo resolvido, nao texto.

        `PRAGMA main.x` versus `PRAGMA x` foi o defeito 8 do gauntlet -- uma
        barreira por texto cai na primeira variacao de escrita. Aqui as duas
        grafias tem de bater no mesmo arquivo.
        """
        relativo = Path("data") / "crypto_traders.db"
        assert _e_o_banco_do_ensaio(RAIZ_DO_REPO / relativo)
        assert _e_o_banco_do_ensaio(f"{RAIZ_DO_REPO.as_posix()}/./data/crypto_traders.db")
        with pytest.raises(BancoDoEnsaioTocado):
            sqlite3.connect(f"{RAIZ_DO_REPO.as_posix()}/./data/crypto_traders.db")

    def test_a_barreira_nao_atrapalha_banco_descartavel(self, tmp_path):
        """Uma barreira que barrasse tudo seria trocada por ninguem."""
        alvo = tmp_path / "crypto_traders.db"
        conexao = sqlite3.connect(str(alvo))
        conexao.close()
        assert alvo.exists()
        assert not _e_o_banco_do_ensaio(alvo)
        assert not _e_o_banco_do_ensaio(":memory:")
        assert not _e_o_banco_do_ensaio("sqlite+aiosqlite:///:memory:")
        assert not _e_o_banco_do_ensaio(None)

    async def test_a_fixture_padrao_escreve_em_tmp_path_e_nao_no_ensaio(
        self, settings, tmp_path
    ):
        """Uma gravacao de verdade, e o arquivo do ensaio intacto byte a byte."""
        antes = (
            BANCO_DO_ENSAIO.stat().st_size if BANCO_DO_ENSAIO.exists() else None
        )

        async with session_scope(settings) as sessao:
            await TradeRepository(sessao).record(
                executed_at=datetime(2026, 1, 1, tzinfo=UTC),
                exchange="paper",
                symbol="BTC/USDT",
                side=str(Side.BUY),
                quantity=Decimal("1"),
                price=Decimal("100"),
                mode="dry_run",
            )
        async with session_scope(settings) as sessao:
            assert len(await TradeRepository(sessao).list()) == 1

        arquivo = Path(settings.database_url.split("///", 1)[1])
        assert arquivo.parent == tmp_path
        assert arquivo.exists()
        depois = (
            BANCO_DO_ENSAIO.stat().st_size if BANCO_DO_ENSAIO.exists() else None
        )
        assert depois == antes, "o banco do ensaio mudou de tamanho durante o teste"


# ---------------------------------------------------------------------------
# 2. A exchange
# ---------------------------------------------------------------------------


class _ClienteQueFalha:
    """Dublê de cliente ccxt: responde o que a exchange responderia de ruim.

    Substitui `CcxtExchange._client`, entao o codigo exercitado e o de producao
    inteiro -- `place_order`, `_with_retry`, a classificacao de erro e o mapa de
    status. So a borda da rede e dublê.
    """

    def __init__(self, erro: Exception | None = None, resposta: dict | None = None):
        self._erro = erro
        self._resposta = resposta
        self.chamadas = 0
        self.sandbox = False

    def set_sandbox_mode(self, ligado: bool) -> None:
        self.sandbox = ligado

    async def create_order(self, *args, **kwargs):
        self.chamadas += 1
        if self._erro is not None:
            raise self._erro
        return self._resposta

    async def close(self) -> None:
        return None


def _credencial_de_teste() -> ExchangeCredentials:
    """Credencial SINTETICA. Nao existe chave real nesta suite, por construcao."""
    return ExchangeCredentials(api_key="chave-de-teste", api_secret="segredo-de-teste")


def _pedido() -> OrderRequest:
    return OrderRequest(
        client_order_id="iso-1",
        signal_id=None,
        risk_event_id="risco-1",
        exchange=ExchangeName.BINANCE,
        symbol="BTC/USDC",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.001"),
        notional=Decimal("50"),
        stop_loss=Decimal("48"),
        strategy="teste",
    )


@asynccontextmanager
async def _adaptador(cliente: _ClienteQueFalha, *, max_retries: int = 1):
    """Adaptador real em modo testnet, com a borda de rede dublada.

    O cliente ccxt de verdade e construido (para que `set_sandbox_mode` rode de
    fato) e fechado no fim, senao a suite deixaria sessao HTTP aberta a cada
    teste.
    """
    adaptador = CcxtExchange(
        "binance", _credencial_de_teste(), testnet=True, max_retries=max_retries
    )
    real = adaptador._client
    adaptador._client = cliente
    try:
        yield adaptador
    finally:
        await real.close()


class TestContraAExchange:
    """Timeout, ordem recusada e saldo insuficiente na borda do adaptador ccxt.

    NAO MEDIDO CONTRA SERVIDOR REAL. Nao ha credencial de testnet nesta maquina,
    e pedir uma seria pedir chave de API -- o que esta suite nao faz. Fica
    explicitamente sem medicao contra a Binance Spot Testnet:

    * o formato exato do erro de saldo insuficiente daquele servidor (aqui e a
      excecao que o ccxt levanta, `ccxt.InsufficientFunds`, que e o que o
      adaptador realmente ve);
    * a latencia real de um timeout e o comportamento do rate limit;
    * se uma ordem aceita e depois recusada assincronamente pela testnet chega
      pelo mesmo campo `status`;
    * se `set_sandbox_mode(True)` aponta para a URL de sandbox que a Binance
      opera hoje -- aqui se prova apenas que o adaptador CHAMA o modo sandbox.

    O que ESTA medido e a reacao do nosso codigo a cada uma das tres respostas.
    """

    async def test_o_modo_testnet_e_realmente_acionado_no_cliente(self, monkeypatch):
        """`testnet=True` tem de virar CHAMADA no cliente, nao intencao.

        Troca a fabrica do ccxt pelo dublê e confere que o construtor chamou
        `set_sandbox_mode(True)`. Sem isto, "testnet" poderia ser um atributo
        guardado que ninguem le -- a familia de defeito que este projeto chama de
        "configurado e inerte". QUAL URL a Binance opera hoje para o sandbox nao
        esta medido aqui: isso exigiria o servidor.
        """
        criados: list[_ClienteQueFalha] = []

        def fabrica(config):
            cliente = _ClienteQueFalha()
            cliente.config = config
            criados.append(cliente)
            return cliente

        monkeypatch.setattr(
            "crypto_traders.exchanges.ccxt_adapter.ccxt.binance", fabrica, raising=False
        )

        ligado = CcxtExchange("binance", _credencial_de_teste(), testnet=True)
        assert ligado._testnet is True
        assert criados[-1].sandbox is True

        desligado = CcxtExchange("binance", _credencial_de_teste(), testnet=False)
        assert desligado._testnet is False
        assert criados[-1].sandbox is False

    async def test_timeout_e_retentado_com_backoff_e_termina_como_falha(
        self, monkeypatch
    ):
        """Timeout e transitorio: retenta, e se insistir NAO vira FILLED.

        O status importa mais que a excecao: uma ordem declarada preenchida sem
        confirmacao criaria posicao inexistente no historico, e e do historico
        que sai o preco medio que arma o stop.
        """
        dormidas: list[float] = []

        async def sem_esperar(segundos):
            dormidas.append(segundos)

        # `asyncio` e o mesmo objeto de modulo em todo lugar, entao este patch
        # alcanca tambem o `close()` do ccxt -- por isso a assercao le so as
        # duas primeiras esperas, que sao as do backoff.
        monkeypatch.setattr("crypto_traders.exchanges.ccxt_adapter.asyncio.sleep", sem_esperar)

        cliente = _ClienteQueFalha(ccxt.RequestTimeout("request timeout"))
        async with _adaptador(cliente, max_retries=3) as adaptador:
            resultado = await adaptador.place_order(_pedido())
            do_backoff = list(dormidas)

        assert cliente.chamadas == 3, "timeout tem de ser retentado ate o limite"
        assert do_backoff == [1.0, 2.0], "o backoff exponencial nao aconteceu"
        assert resultado.status is OrderStatus.FAILED
        assert resultado.status is not OrderStatus.FILLED
        assert "timeout" in (resultado.error or "").lower()

    async def test_ordem_recusada_pela_exchange_chega_como_REJECTED(self):
        """`status: rejected` no corpo da resposta, nao excecao."""
        cliente = _ClienteQueFalha(
            resposta={"id": "9", "status": "rejected", "filled": 0, "average": None}
        )
        async with _adaptador(cliente) as adaptador:
            resultado = await adaptador.place_order(_pedido())

        assert resultado.status is OrderStatus.REJECTED
        assert resultado.filled_quantity == Decimal(0)
        assert resultado.exchange_order_id == "9"

    async def test_status_desconhecido_nunca_e_lido_como_preenchida(self):
        """O estado seguro nao e "arrisca menos", e "nao arrisca"."""
        cliente = _ClienteQueFalha(
            resposta={"id": "9", "status": "algo_que_a_binance_inventou", "filled": 0}
        )
        async with _adaptador(cliente) as adaptador:
            resultado = await adaptador.place_order(_pedido())
        assert resultado.status is OrderStatus.OPEN
        assert resultado.status is not OrderStatus.FILLED

    async def test_sem_credencial_a_ordem_nem_sai(self):
        """Antes de qualquer rede: sem chave configurada, nao ha envio."""
        adaptador = CcxtExchange("binance", None, testnet=True)
        try:
            with pytest.raises(ExchangeError, match="sem credenciais"):
                await adaptador.place_order(_pedido())
        finally:
            await adaptador._client.close()

    async def test_saldo_insuficiente_nao_e_retentado(self):
        """Erro de negocio: a exchange respondeu "nao", retentar so atrasa."""
        cliente = _ClienteQueFalha(ccxt.InsufficientFunds("saldo insuficiente"))
        async with _adaptador(cliente, max_retries=3) as adaptador:
            await adaptador.place_order(_pedido())
        assert cliente.chamadas == 1, "erro de negocio nao pode ser retentado"


class TestSaldoInsuficienteNoBrokerDeVerdade:
    """O ramo que existe e nunca roda com o broker que a producao usa.

    `ExecutionAgent._execute` tem `except InsufficientFunds` -> `_rejected`, com
    o comentario "a exchange respondeu, e a resposta foi nao". A suite prova esse
    ramo com `BrokerProgramado`, um dublê que LEVANTA a excecao
    (`test_execution.py::TestSaldoInsuficienteNoEnvio`).

    Nenhum dos dois brokers reais levanta:

    * `PaperBroker.place_order` captura o proprio `InsufficientFunds` e devolve
      `OrderResult(REJECTED)`;
    * `CcxtExchange.place_order` captura `ExchangeError` -- e
      `InsufficientFunds` HERDA de `ExchangeError` -- e devolve
      `OrderResult(FAILED)`.

    Com o broker real da exchange o desfecho e FAILED, e a diferenca custa: o
    cooldown do Risk Manager (`OrderRepository.last_order_time`) exclui APENAS
    `REJECTED`. Uma recusa que nao chegou ao mercado, vinda da Binance, queima o
    cooldown do par; a mesma recusa vinda do dublê nao queima. Isso e o cenario
    de mandato 5 (saldo insuficiente) medido no lugar onde a producao realmente
    passa.
    """

    async def test_o_adaptador_real_devolve_FAILED_e_nao_levanta(self):
        """Realidade MEDIDA, nao desejada: o que a producao recebe hoje."""
        cliente = _ClienteQueFalha(ccxt.InsufficientFunds("saldo insuficiente de USDC"))
        async with _adaptador(cliente) as adaptador:
            resultado = await adaptador.place_order(_pedido())

        assert resultado.status is OrderStatus.FAILED
        assert resultado.status is not OrderStatus.REJECTED
        assert "insuficiente" in (resultado.error or "")

    async def test_o_paper_broker_devolve_REJECTED_e_tambem_nao_levanta(
        self, settings
    ):
        """O outro broker real: mesmo formato de resposta, status diferente."""
        from crypto_traders.exchanges.paper import PaperBroker

        broker = PaperBroker(quote_currency="USDC", initial_balance=Decimal("1"))
        broker.set_price("BTC", Decimal("50000"))
        resultado = await broker.place_order(_pedido())
        assert resultado.status is OrderStatus.REJECTED

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "DEFEITO MEDIDO 2026-09-10: com o broker real da exchange, saldo "
            "insuficiente chega ao ExecutionAgent como FAILED, nunca como "
            "REJECTED, porque CcxtExchange.place_order captura ExchangeError e "
            "InsufficientFunds herda dele. O ramo `except InsufficientFunds` do "
            "agente e inalcancavel em producao, e o cooldown do Risk Manager "
            "(que ignora so REJECTED) fica armado por uma ordem que nunca chegou "
            "ao mercado. O conserto e em exchanges/ccxt_adapter.py, fora do "
            "escopo deste item -- xfail estrito para virar vermelho quando for "
            "consertado."
        ),
    )
    async def test_deveria_chegar_ao_agente_como_recusa_definitiva(self, settings):
        from crypto_traders.agents.execution import ExecutionAgent
        from crypto_traders.bus import InMemoryEventBus

        cliente = _ClienteQueFalha(ccxt.InsufficientFunds("saldo insuficiente de USDC"))
        async with _adaptador(cliente) as adaptador:
            agente = ExecutionAgent(InMemoryEventBus(), adaptador, settings)
            resultado = await agente._execute(_pedido())

        assert resultado is not None
        assert resultado.status is OrderStatus.REJECTED


# ---------------------------------------------------------------------------
# 3. O diretorio de trabalho
# ---------------------------------------------------------------------------


class TestASuiteNaoDependeDoDiretorioDeTrabalho:
    """Medido em 2026-09-10: a suite tem 1118 verdes de `backend/` e 1 vermelho
    da raiz do repositorio.

    `test_critico_item2_ataques.py` abre `pathlib.Path("src/crypto_traders/...")`,
    que so existe se o `cd` de quem roda for `backend/`. Aquele arquivo e de
    outro item e esta verde no caminho documentado, entao nao e reescrito aqui --
    o helper ancorado existe para que o proximo teste nao repita o erro, e o
    achado fica relatado.
    """

    def test_o_helper_ancorado_encontra_o_fonte_de_qualquer_diretorio(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        alvo = arquivo_do_repo("src/crypto_traders/agents/strategy.py")
        assert alvo.exists()
        assert "class StrategyAgent" in alvo.read_text(encoding="utf-8")

    def test_o_caminho_relativo_e_justamente_o_que_nao_sobrevive(
        self, tmp_path, monkeypatch
    ):
        """A contraprova: sem a ancora, o mesmo caminho desaparece."""
        monkeypatch.chdir(tmp_path)
        assert not Path("src/crypto_traders/agents/strategy.py").exists()
