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

from .config import RiskSettings, Settings, TradingSettings
from .db.repositories import AuditLogRepository, RiskConfigRepository, TradingConfigRepository
from .db.session import session_scope
from .logging_setup import get_logger

log = get_logger(__name__)


async def load_business_config(settings: Settings) -> tuple[TradingSettings, RiskSettings]:
    """Le negocio do banco, semeando com os padroes do modelo na primeira vez.

    Uma linha invalida (campo removido do modelo, valor corrompido) **nao**
    derruba a subida: caimos nos padroes conservadores e registramos erro. O
    raciocinio e o mesmo do resto do projeto -- o estado alternativo aqui nao e
    "arriscado", e "arrisca de menos".
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
    """
    atual = settings.trading
    combinado = {**atual.model_dump(mode="json"), **changes}
    novo = TradingSettings.model_validate(combinado)

    if novo == atual:
        return atual

    antes = {campo: atual.model_dump(mode="json")[campo] for campo in changes}
    depois = {campo: novo.model_dump(mode="json")[campo] for campo in changes}
    await _persistir_trading(settings, novo, actor=actor, antes=antes, depois=depois)

    settings.with_business_config(novo, settings.risk)
    log.info("config.trading_updated", campos=sorted(changes), actor=actor)
    return novo


def _validar(modelo: type, gravado: dict | None, alvo: str):
    if not gravado:
        return modelo()
    try:
        return modelo.model_validate(gravado)
    except Exception as exc:
        log.error(
            "config.stored_invalid",
            target=alvo,
            error=str(exc),
            detail="usando os padroes conservadores do modelo",
        )
        return modelo()


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
