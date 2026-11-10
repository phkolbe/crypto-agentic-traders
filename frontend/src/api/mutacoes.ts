/**
 * Pedidos que mudam estado, montados num lugar so — e conferidos contra a API.
 *
 * "Rearmar" e "Autorizar capital" mandaram POST sem corpo de 10/09 a 09/11/2026:
 * a API passou a exigir `confirm` (3885f7d) e a tela nao acompanhou, entao os dois
 * botoes responderam 400 por dois meses. Tipo certo, contrato errado — e nenhum
 * teste cruzava tela e API.
 *
 * Por isso os corpos nascem aqui, em funcoes puras sem `fetch` nem `import.meta`:
 * o `npm run check` as chama e compara com `checks/contrato-mutacoes.json`, e o
 * `backend/tests/test_api.py` (TestContratoDasTelas) repete esse mesmo arquivo
 * contra a API. O `client.ts` so envia o que sai daqui.
 *
 * `confirm: true` aqui NAO e a confirmacao: e o registro de que ela aconteceu.
 * Cada funcao deste arquivo so pode ser chamada depois de um passo explicito na
 * tela (o dialogo que diz o que vai acontecer) — e e assim que as paginas as usam.
 */

import type { Money } from './types'

export type Metodo = 'POST' | 'PUT' | 'DELETE'

export interface Pedido {
  metodo: Metodo
  /** Caminho debaixo de `RAIZ/api`, com a query quando houver. */
  caminho: string
  corpo?: Record<string, unknown>
}

/** O lancamento como a API o recebe (sem as confirmacoes). */
export interface LancamentoManual {
  executed_at: string
  exchange: string
  symbol: string
  side: string
  quantity: string
  price: string
  fee: string
  fee_currency: string | null
  notes: string | null
}

/** O formulario da tela Lancar manual, como o usuario o deixou. */
export interface FormularioDeLancamento {
  executed_at: string
  exchange: string
  symbol: string
  side: string
  quantity: string
  price: string
  fee: string
  fee_currency: string
  notes: string
}

/** `datetime-local` espera o horario LOCAL sem fuso; ISO com Z apareceria deslocado. */
export function horarioLocalParaInput(instante: Date): string {
  const local = new Date(instante.getTime() - instante.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 16)
}

/**
 * Formulario vazio, com a data de AGORA — calculada quando o formulario abre.
 *
 * Era uma constante de modulo (`EMPTY.executed_at = nowForInput()`), avaliada
 * quando o app carregava: aberto as 09:00 e usado as 15:00, o campo vinha com
 * 09:00 e o lancamento era gravado 6 h antes, fora de lugar na ordem que reapura
 * o custo medio. `fee_currency` tambem nao tem valor de fabrica: era `'USDT'`, e
 * depois da migracao para USDC todo lancamento salvo sem tocar no campo gravava
 * a moeda errada. O padrao e a cotacao do par, resolvida no envio.
 */
export function formularioVazio(agora: Date): FormularioDeLancamento {
  return {
    executed_at: horarioLocalParaInput(agora),
    exchange: 'binance',
    symbol: '',
    side: 'buy',
    quantity: '',
    price: '',
    fee: '',
    fee_currency: '',
    notes: '',
  }
}

/**
 * Formulario -> corpo da API.
 *
 * O `datetime-local` entrega o horario LOCAL sem fuso; `new Date(...)` o
 * interpreta no fuso do navegador e `toISOString` o devolve em UTC com `Z`. A API
 * recusa data sem fuso (seria gravada 3 h fora do lugar na ordem que reapura o
 * custo medio).
 */
export function lancamentoDoFormulario(
  form: FormularioDeLancamento,
  moedaDoPar: string,
): LancamentoManual {
  return {
    executed_at: new Date(form.executed_at).toISOString(),
    exchange: form.exchange,
    symbol: form.symbol.toUpperCase(),
    side: form.side,
    quantity: form.quantity,
    price: form.price,
    fee: form.fee || '0',
    fee_currency: form.fee_currency || moedaDoPar || null,
    notes: form.notes || null,
  }
}

export const pedidos = {
  /** Depois do dialogo "rearmar devolve ao sistema a permissao de abrir posicoes". */
  rearmarCircuitBreaker: (): Pedido => ({
    metodo: 'POST',
    caminho: '/risk/circuit-breaker/reset',
    corpo: { confirm: true },
  }),

  /**
   * Depois do dialogo "autorizar X". `patrimonioVisto` e o total que a tela
   * mostrou: se um deposito chegou entre carregar e clicar, a API recusa (409)
   * em vez de autorizar o que apareceu na hora.
   */
  autorizarCapital: (patrimonioVisto: Money): Pedido => ({
    metodo: 'POST',
    caminho: '/risk/capital/authorize',
    corpo: { confirm: true, expected_total_value: patrimonioVisto },
  }),

  /** Depois do "Sim, aplicar" da tela Risco. */
  alterarLimites: (alteracoes: Record<string, unknown>): Pedido => ({
    metodo: 'PUT',
    caminho: '/risk/config',
    corpo: { ...alteracoes, confirm: true },
  }),

  /** Depois do "Sim, aplicar" da tela Configuracoes. */
  alterarNegocio: (alteracoes: Record<string, unknown>): Pedido => ({
    metodo: 'PUT',
    caminho: '/trading/config',
    corpo: { ...alteracoes, confirm: true },
  }),

  /**
   * Depois do resumo "isto entra no preco medio e move o stop". O segundo
   * argumento so vira `true` num segundo passo, quando a API respondeu 409 por
   * preco longe do mercado e o operador confirmou que o preco esta certo.
   */
  lancarManual: (lancamento: LancamentoManual, aceitarPrecoForaDoMercado: boolean): Pedido => ({
    metodo: 'POST',
    caminho: '/trades/manual',
    corpo: {
      ...lancamento,
      confirm: true,
      aceitar_preco_fora_do_mercado: aceitarPrecoForaDoMercado,
    },
  }),

  /** Depois do "excluir este lancamento?" da linha. Na query: corpo em DELETE nao e confiavel. */
  excluirLancamento: (id: string): Pedido => ({
    metodo: 'DELETE',
    caminho: `/trades/${encodeURIComponent(id)}?confirm=true`,
  }),
}
