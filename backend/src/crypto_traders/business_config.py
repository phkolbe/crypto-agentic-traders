"""Carrega e altera a configuracao de negocio, que mora no banco.

O `.env` descreve a instalacao; este modulo cuida de tudo que responde "o que eu
quero negociar e com quanto risco". A separacao esta explicada em `config.py`.

Duas garantias que este modulo existe para dar:

1. **Uma fonte da verdade.** Ninguem le negocio do ambiente. Se a linha do banco
   nao existe, os padroes vem do modelo Pydantic -- nunca de variavel de ambiente.
2. **Toda alteracao deixa rastro.** Mudar limite de risco ou universo negociado
   e decisao sobre dinheiro real, entao passa por validacao, vai para o
   `audit_log` com o antes e o depois, e so entao vale.
"""

from __future__ import annotations

import re

from .config import RiskSettings, Settings, TradingSettings
from .db.repositories import AuditLogRepository, RiskConfigRepository, TradingConfigRepository
from .db.session import session_scope
from .logging_setup import get_logger

log = get_logger(__name__)


class ConfiguracaoGravadaInvalida(RuntimeError):
    """A configuracao de negocio gravada nao valida: o sistema nao sobe.

    Nao existe padrao seguro para "o que negociar". Os padroes do modelo eram o
    estado alternativo e foram descritos aqui como "arrisca de menos" -- falso:
    `TradingSettings()` e USDT, 15m, `ma_crossover` + `rsi_reversion`, contra o
    USDC/1d/so `ma_crossover` do dry_run. Subir com eles e operar outra moeda e
    outra estrategia, e deixar as posicoes em USDC sem preco para o stop
    (auditoria de 09/11/2026, P0 #10). Recusar a subida derruba o healthcheck,
    e o `implantar.sh` mostra o erro ao operador.
    """


async def load_business_config(settings: Settings) -> tuple[TradingSettings, RiskSettings]:
    """Le negocio do banco, semeando com os padroes do modelo na primeira vez.

    Uma atualizacao do codigo que remove ou acrescenta um campo nao pode
    derrubar o que esta gravado: campo desconhecido e ignorado (com aviso) e
    campo ausente recebe o padrao do modelo, preservando todo o resto. Se mesmo
    assim a linha nao valida (valor corrompido, estrategia que saiu do codigo),
    levanta `ConfiguracaoGravadaInvalida` -- o sistema nao sobe, em vez de subir
    operando os padroes de fabrica.
    """
    async with session_scope(settings) as session:
        gravado_trading = await TradingConfigRepository(session).get()
        gravado_risk = dict((await RiskConfigRepository(session).get_or_create({})).values or {})

    trading = _validar(TradingSettings, gravado_trading, "trading_config")
    risk = _validar(RiskSettings, gravado_risk, "risk_config")

    if gravado_trading is None:
        # Primeira subida: grava os padroes para que a interface tenha o que
        # editar e o proximo start nao dependa de defaults de codigo.
        await _persistir_trading(settings, trading, actor="system", detalhe="semeadura inicial")

    return trading, risk


async def install_business_config(settings: Settings) -> None:
    """Carrega do banco e instala em `settings`, no lugar."""
    trading, risk = await load_business_config(settings)
    settings.with_business_config(trading, risk)
    log.info(
        "config.business_loaded",
        pares=len(trading.symbols) or "descoberta",
        timeframe=trading.timeframe,
        estrategias=trading.strategies,
        moeda=trading.quote_currency,
    )


async def update_trading_config(
    settings: Settings, changes: dict, *, actor: str = "user"
) -> TradingSettings:
    """Valida, grava e instala uma alteracao parcial da configuracao de negocio.

    Levanta `ValueError` quando a combinacao resultante e invalida -- e a
    validacao roda sobre o resultado do merge, nao sobre o pedaco enviado: um
    campo isolado pode ser valido e ainda assim tornar o conjunto incoerente.

    O `audit_log` recebe o antes e o depois COMPLETOS. Gravar so os campos
    enviados escondia o que de fato mudou: o merge persiste a configuracao
    inteira, e se a base em memoria divergisse do banco, todos os outros campos
    seriam sobrescritos sem rastro.
    """
    atual = settings.trading
    combinado = {**atual.model_dump(mode="json"), **changes}
    novo = TradingSettings.model_validate(combinado)
    _validar_operavel(novo)

    if novo == atual:
        return atual

    antes = atual.model_dump(mode="json")
    depois = novo.model_dump(mode="json")
    alterados = sorted(campo for campo in depois if depois[campo] != antes.get(campo))
    await _persistir_trading(
        settings, novo, actor=actor, antes=antes, depois=depois,
        detalhe=f"campos alterados: {', '.join(alterados)}",
    )

    settings.with_business_config(novo, settings.risk)
    log.info("config.trading_updated", campos=sorted(changes), actor=actor)
    return novo


def _validar_operavel(trading: TradingSettings) -> None:
    """Recusa, na edicao, configuracao que o sistema aceita mas nao consegue operar.

    O modelo so conferia estrategias. Medido: `{"timeframe": "1x"}` voltava 200,
    a coleta de candles falhava a cada ciclo e nenhum sinal saia, com a tela de
    configuracao "verde". E `{"symbols": ["BTC/USDT"]}` com a cotacao em USDC
    tambem: o caixa medido numa moeda, o par em outra, e o ativo em carteira
    cujo par saiu do universo perde o stop.

    Vale so na EDICAO, de proposito. Na subida, uma linha antiga com par de outra
    cotacao continua carregando (o `MarketDataAgent` guarda o preco dela sob o
    nome completo do par, ver `_chave_de_preco`); recusar a subida por isso
    seria trocar um aviso por um sistema parado com posicao aberta.
    """
    # Import local: `cli` importa `config` e o banco; aqui so precisamos da
    # tabela de timeframes, a mesma que o backtest usa.
    from .cli import TIMEFRAME_MINUTES

    if trading.timeframe not in TIMEFRAME_MINUTES:
        raise ValueError(
            f"timeframe {trading.timeframe!r} invalido; use um de: "
            f"{', '.join(TIMEFRAME_MINUTES)}"
        )
    moeda = trading.quote_currency.upper()
    fora = [
        par for par in trading.symbols
        if not re.fullmatch(rf"[A-Z0-9]+/{re.escape(moeda)}", par)
    ]
    if fora:
        raise ValueError(
            f"par(es) fora da moeda de cotacao {moeda}: {', '.join(fora)}. "
            f"Use BASE/{moeda} em maiusculas; para trocar a moeda, envie "
            "quote_currency e symbols juntos"
        )


def _validar(modelo: type, gravado: dict | None, alvo: str):
    """Valida a linha gravada, tolerando a evolucao do modelo -- e so isso.

    Campo que saiu do modelo e ignorado com aviso (a linha no banco NAO e
    reescrita: voltar a versao anterior do codigo o reencontra). Campo novo
    recebe o padrao do modelo pelo proprio Pydantic. O que nao valida depois
    disso levanta `ConfiguracaoGravadaInvalida`: ver a classe para o porque.
    """
    if not gravado:
        return modelo()
    conhecidos = {k: v for k, v in gravado.items() if k in modelo.model_fields}
    desconhecidos = sorted(set(gravado) - set(conhecidos))
    if desconhecidos:
        log.warning(
            "config.stored_unknown_fields",
            target=alvo,
            campos=desconhecidos,
            detail="campos que o codigo atual nao conhece; ignorados, o resto vale",
        )
    ausentes = sorted(set(modelo.model_fields) - set(gravado))
    if ausentes:
        log.warning(
            "config.stored_missing_fields",
            target=alvo,
            campos=ausentes,
            detail="campos novos no codigo; recebem o padrao do modelo",
        )
    try:
        return modelo.model_validate(conhecidos)
    except Exception as exc:
        log.error("config.stored_invalid", target=alvo, error=str(exc))
        raise ConfiguracaoGravadaInvalida(
            f"a configuracao gravada em {alvo} nao valida e o sistema nao sobe "
            f"com padroes de fabrica no lugar dela. Corrija a linha no banco "
            f"(ou volte a versao anterior do codigo). Erro: {exc}"
        ) from exc


async def _persistir_trading(
    settings: Settings,
    trading: TradingSettings,
    *,
    actor: str,
    antes: dict | None = None,
    depois: dict | None = None,
    detalhe: str | None = None,
) -> None:
    async with session_scope(settings) as session:
        await TradingConfigRepository(session).save(trading.model_dump(mode="json"))
        await AuditLogRepository(session).append(
            actor=actor,
            action="trading_config_updated",
            target="trading_config",
            before=antes or {},
            after=depois or {},
            detail=detalhe,
        )
