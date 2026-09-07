"""Motor de regras de risco.

Logica **pura**: sem banco, sem rede, sem event bus. Recebe um sinal e um retrato
do portfolio, devolve um veredito. Essa pureza e proposital -- e o componente que
decide se dinheiro real e usado, entao precisa ser testavel exaustivamente sem
nenhum mock de infraestrutura.

O mesmo motor e usado pelo Risk Manager Agent em producao e pelo backtest. Se as
regras divergissem entre os dois, o backtest estaria validando um sistema que nao
existe.

Assimetrias deliberadas entre ABRIR e FECHAR posicao:

- Cooldown e circuit breaker **bloqueiam abertura**, nunca fechamento. Impedir
  uma saida durante uma queda -- exatamente quando o circuit breaker dispara --
  transformaria uma protecao em armadilha.
- Notional minimo vale so para abertura. Uma posicao pequena precisa poder ser
  fechada por inteiro, senao vira poeira presa na carteira para sempre.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from ..config import RiskSettings
from ..domain.enums import RiskDecision, SignalDirection
from ..domain.models import RiskAssessment, Signal

#: Precisao de quantidade. A exchange aplica a sua propria (stepSize); arredondar
#: para baixo aqui garante que nunca pedimos mais do que o permitido pelo limite.
QUANTITY_PRECISION = Decimal("0.00000001")


@dataclass
class PortfolioState:
    """Retrato do portfolio no instante da avaliacao."""

    total_value: Decimal
    """Patrimonio total na moeda de cotacao (caixa + posicoes)."""

    cash: Decimal
    positions: dict[str, Decimal] = field(default_factory=dict)
    """Ativo base -> quantidade detida."""

    prices: dict[str, Decimal] = field(default_factory=dict)
    """Ativo base -> preco na moeda de cotacao."""

    last_order_at: dict[str, datetime] = field(default_factory=dict)
    """Simbolo -> horario da ultima ordem, para o cooldown."""

    circuit_breaker_active: bool = False
    circuit_breaker_reason: str | None = None

    def quantity_of(self, asset: str) -> Decimal:
        return self.positions.get(asset, Decimal(0))

    def value_of(self, asset: str) -> Decimal:
        price = self.prices.get(asset)
        if price is None:
            return Decimal(0)
        return self.quantity_of(asset) * price

    def open_positions(self, quote_currency: str, dust_threshold: Decimal) -> int:
        """Conta posicoes relevantes, ignorando poeira e o proprio caixa."""
        return sum(
            1
            for asset in self.positions
            if asset != quote_currency and self.value_of(asset) >= dust_threshold
        )


@dataclass(frozen=True)
class SizingFeasibility:
    """Diagnostico: com estes limites e este patrimonio, alguma ordem e possivel?

    Existe porque a combinacao "carteira pequena + limites conservadores" produz
    o pior estado do sistema: ele sobe, coleta dados, gera sinais e rejeita
    TODOS em silencio -- heartbeat verde, dashboard atualizando, e o motivo
    enterrado numa mensagem tecnica em `risk_events`. Parece saudavel e nunca
    vai operar.

    Exemplo real: R$100 (~19 USDT) com os padroes de fabrica da 2% por ordem, ou
    seja 39 centavos -- abaixo do minimo de 10 USDT e do proprio minimo da
    Binance (5 USDT). Nenhuma ordem jamais sairia.
    """

    feasible: bool
    portfolio_value: Decimal
    max_possible_order: Decimal
    min_order_notional: Decimal
    binding_limit: str
    """Qual limite trava o dimensionamento no melhor cenario possivel."""

    minimum_portfolio: Decimal
    """Patrimonio minimo para que uma ordem passe, sem mexer em nenhum limite."""

    def explain(self, quote_currency: str = "USDT") -> str:
        if self.feasible:
            return (
                f"maior ordem possivel: {self.max_possible_order:.2f} {quote_currency} "
                f"(limitada por {self.binding_limit})"
            )
        return (
            f"NENHUMA ordem e possivel: o patrimonio de {self.portfolio_value:.2f} "
            f"{quote_currency} permite no maximo {self.max_possible_order:.2f} "
            f"(travado por {self.binding_limit}), mas o minimo por ordem e "
            f"{self.min_order_notional:.2f}. Seriam necessarios ao menos "
            f"{self.minimum_portfolio:.2f} {quote_currency} para operar com os "
            f"limites atuais."
        )


def assess_sizing_feasibility(
    limits: RiskSettings, portfolio_value: Decimal
) -> SizingFeasibility:
    """Avalia o MELHOR cenario: carteira toda em caixa, sem posicao no ativo.

    Se nem assim uma ordem passa, nenhuma passara jamais -- e o operador precisa
    saber disso na subida, nao depois de dias olhando um dashboard que nunca
    registra operacao.
    """
    pct_cap = portfolio_value * Decimal(str(limits.max_order_pct_portfolio))
    exposure_cap = portfolio_value * Decimal(str(limits.max_asset_exposure_pct))

    candidates = {
        "teto absoluto por ordem": limits.max_order_notional,
        f"{limits.max_order_pct_portfolio:.0%} do portfolio por ordem": pct_cap,
        f"exposicao maxima de {limits.max_asset_exposure_pct:.0%} por ativo": exposure_cap,
    }
    binding_limit, max_possible = min(candidates.items(), key=lambda item: item[1])

    # O gargalo e sempre o menor entre o percentual por ordem e a exposicao
    # maxima -- foi a exposicao que travou o caso de R$100 mesmo depois de
    # afrouxar o percentual, e apontar o limite errado manda investigar em vao.
    tightest_pct = Decimal(
        str(min(limits.max_order_pct_portfolio, limits.max_asset_exposure_pct))
    )
    minimum_portfolio = (limits.min_order_notional / tightest_pct).quantize(Decimal("0.01"))

    return SizingFeasibility(
        feasible=max_possible >= limits.min_order_notional,
        portfolio_value=portfolio_value,
        max_possible_order=max_possible,
        min_order_notional=limits.min_order_notional,
        binding_limit=binding_limit,
        minimum_portfolio=minimum_portfolio,
    )


class RiskEngine:
    """Aplica todas as regras de risco a um sinal."""

    def __init__(self, limits: RiskSettings, quote_currency: str = "USDT") -> None:
        self.limits = limits
        self.quote_currency = quote_currency

    # ------------------------------------------------------------------
    def evaluate(
        self, signal: Signal, state: PortfolioState, now: datetime | None = None
    ) -> RiskAssessment:
        now = now or datetime.now(UTC)
        base, quote = _split_symbol(signal.symbol)

        if signal.direction is SignalDirection.LONG:
            return self._evaluate_open(signal, state, base, quote, now)
        if signal.direction is SignalDirection.FLAT:
            return self._evaluate_close(signal, state, base, now)

        # SHORT nao existe no MVP: operamos spot, onde vender a descoberto nao e
        # possivel. Rejeitar explicitamente evita que uma estrategia futura
        # produza ordens sem sentido silenciosamente.
        return self._reject(
            signal,
            ["venda a descoberto (SHORT) nao e suportada em spot"],
            state,
        )

    # ------------------------------------------------------------------
    # Abertura / aumento de posicao
    # ------------------------------------------------------------------
    def _evaluate_open(
        self,
        signal: Signal,
        state: PortfolioState,
        base: str,
        quote: str,
        now: datetime,
    ) -> RiskAssessment:
        limits = self.limits
        reasons: list[str] = []

        if state.circuit_breaker_active:
            reasons.append(
                f"circuit breaker ativo: {state.circuit_breaker_reason or 'sem detalhe'}"
            )

        if signal.symbol not in limits.symbol_whitelist:
            reasons.append(f"simbolo '{signal.symbol}' fora da whitelist")
        if base not in limits.asset_whitelist:
            reasons.append(f"ativo '{base}' fora da whitelist")
        if quote != self.quote_currency:
            reasons.append(
                f"moeda de cotacao '{quote}' difere da configurada '{self.quote_currency}'"
            )

        if signal.confidence < limits.min_signal_confidence:
            reasons.append(
                f"confianca {signal.confidence:.2f} abaixo do minimo "
                f"{limits.min_signal_confidence:.2f}"
            )

        last_order = state.last_order_at.get(signal.symbol)
        if last_order is not None and limits.cooldown_seconds > 0:
            elapsed = now - _aware(last_order)
            if elapsed < timedelta(seconds=limits.cooldown_seconds):
                remaining = timedelta(seconds=limits.cooldown_seconds) - elapsed
                reasons.append(
                    f"cooldown ativo para {signal.symbol}: faltam {int(remaining.total_seconds())}s"
                )

        dust = limits.min_order_notional / 2
        already_held = state.quantity_of(base) > 0
        # Aumentar uma posicao existente nao abre uma nova, entao o limite de
        # posicoes abertas nao deve bloquear esse caso.
        if not already_held:
            open_count = state.open_positions(self.quote_currency, dust)
            if open_count >= limits.max_open_positions:
                reasons.append(
                    "limite de posicoes abertas atingido "
                    f"({open_count}/{limits.max_open_positions})"
                )

        price = signal.reference_price
        if price <= 0:
            reasons.append("preco de referencia invalido")
            return self._reject(signal, reasons, state)

        if state.total_value <= 0:
            reasons.append("portfolio sem valor apurado; impossivel dimensionar a ordem")
            return self._reject(signal, reasons, state)

        # Tamanho = o MENOR entre o teto absoluto e o teto percentual. Os dois
        # existem porque protegem de coisas diferentes: o percentual acompanha o
        # crescimento da carteira, o absoluto limita o estrago de um erro de
        # calculo no percentual.
        notional = min(
            limits.max_order_notional,
            state.total_value * Decimal(str(limits.max_order_pct_portfolio)),
        )

        # Nunca gastar mais caixa do que existe.
        notional = min(notional, state.cash)

        if notional < limits.min_order_notional:
            reasons.append(
                f"tamanho disponivel {notional:.2f} {self.quote_currency} abaixo do minimo "
                f"{limits.min_order_notional} (caixa: {state.cash:.2f})"
            )

        # Exposicao maxima por ativo, contando o que ja existe.
        exposure_cap = state.total_value * Decimal(str(limits.max_asset_exposure_pct))
        current_exposure = state.value_of(base)
        headroom = exposure_cap - current_exposure
        if headroom <= 0:
            reasons.append(
                f"exposicao em {base} ja em {_pct(current_exposure, state.total_value)} "
                f"(limite {limits.max_asset_exposure_pct:.0%})"
            )
        elif headroom < notional:
            # Nao rejeitamos: reduzimos a ordem ate caber no limite. Rejeitar
            # aqui deixaria a carteira parada perto do teto sem necessidade.
            notional = headroom
            if notional < limits.min_order_notional:
                reasons.append(
                    f"espaco restante de exposicao em {base} ({notional:.2f}) abaixo do "
                    f"minimo de ordem ({limits.min_order_notional})"
                )

        if reasons:
            return self._reject(signal, reasons, state)

        quantity = (notional / price).quantize(QUANTITY_PRECISION, rounding=ROUND_DOWN)
        if quantity <= 0:
            return self._reject(signal, ["quantidade calculada arredondou para zero"], state)

        # Recalcula o notional a partir da quantidade ja arredondada, para que o
        # valor registrado seja o que realmente sera enviado.
        notional = quantity * price

        stop_loss = price * (Decimal(1) - Decimal(str(limits.stop_loss_pct)))
        take_profit = price * (Decimal(1) + Decimal(str(limits.take_profit_pct)))

        return RiskAssessment(
            signal_id=signal.id,
            decision=RiskDecision.APPROVED,
            reasons=[],
            approved_quantity=quantity,
            approved_notional=notional,
            stop_loss=stop_loss,
            take_profit=take_profit,
            snapshot=self._snapshot(state, signal, base),
        )

    # ------------------------------------------------------------------
    # Fechamento de posicao
    # ------------------------------------------------------------------
    def _evaluate_close(
        self, signal: Signal, state: PortfolioState, base: str, now: datetime
    ) -> RiskAssessment:
        quantity = state.quantity_of(base).quantize(QUANTITY_PRECISION, rounding=ROUND_DOWN)
        if quantity <= 0:
            return self._reject(signal, [f"nao ha posicao em {base} para fechar"], state)

        price = signal.reference_price
        if price <= 0:
            return self._reject(signal, ["preco de referencia invalido"], state)

        # Sem cooldown, sem circuit breaker, sem notional minimo: fechar posicao
        # e sempre permitido. Uma trava que impede a saida deixa de ser protecao.
        return RiskAssessment(
            signal_id=signal.id,
            decision=RiskDecision.APPROVED,
            reasons=[],
            approved_quantity=quantity,
            approved_notional=quantity * price,
            stop_loss=None,
            take_profit=None,
            snapshot=self._snapshot(state, signal, base) | {"closing": True},
        )

    # ------------------------------------------------------------------
    def _reject(
        self, signal: Signal, reasons: list[str], state: PortfolioState
    ) -> RiskAssessment:
        base, _ = _split_symbol(signal.symbol)
        return RiskAssessment(
            signal_id=signal.id,
            decision=RiskDecision.REJECTED,
            reasons=reasons,
            snapshot=self._snapshot(state, signal, base),
        )

    def _snapshot(self, state: PortfolioState, signal: Signal, base: str) -> dict:
        """Estado usado na decisao, congelado para auditoria posterior."""
        return {
            "symbol": signal.symbol,
            "direction": str(signal.direction),
            "confidence": round(signal.confidence, 4),
            "reference_price": str(signal.reference_price),
            "total_value": str(state.total_value),
            "cash": str(state.cash),
            "asset_quantity": str(state.quantity_of(base)),
            "asset_exposure": str(state.value_of(base)),
            "circuit_breaker_active": state.circuit_breaker_active,
            "limits": {
                "max_order_notional": str(self.limits.max_order_notional),
                "max_order_pct_portfolio": self.limits.max_order_pct_portfolio,
                "max_asset_exposure_pct": self.limits.max_asset_exposure_pct,
                "min_signal_confidence": self.limits.min_signal_confidence,
                "cooldown_seconds": self.limits.cooldown_seconds,
            },
        }


def _split_symbol(symbol: str) -> tuple[str, str]:
    base, _, quote = symbol.partition("/")
    return base, quote or "USDT"


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _pct(part: Decimal, whole: Decimal) -> str:
    if whole <= 0:
        return "0%"
    return f"{(part / whole):.1%}"
