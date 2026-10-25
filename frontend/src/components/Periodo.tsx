import { useState } from 'react'

import {
  ATALHOS,
  dataLocal,
  erroDoPersonalizado,
  lerPeriodoSalvo,
  type Periodo,
} from '../api/periodo'

/** Periodo da tela, lembrado no navegador (por tela) entre uma visita e outra. */
export function usePeriodo(chave: string, padrao: Periodo): [Periodo, (p: Periodo) => void] {
  const [periodo, setPeriodo] = useState<Periodo>(() => {
    try {
      return lerPeriodoSalvo(localStorage.getItem(`periodo:${chave}`), padrao, new Date())
    } catch {
      return padrao
    }
  })
  const escolher = (novo: Periodo) => {
    setPeriodo(novo)
    try {
      localStorage.setItem(`periodo:${chave}`, JSON.stringify(novo))
    } catch {
      // Navegador sem armazenamento local: vale so nesta visita.
    }
  }
  return [periodo, escolher]
}

/**
 * Atalhos de periodo e o intervalo personalizado (no maximo 1 ano).
 *
 * As datas so valem ao clicar em Aplicar: aplicar a cada tecla dispararia uma
 * consulta por data incompleta digitada.
 */
export function SeletorDePeriodo({
  periodo,
  onChange,
}: {
  periodo: Periodo
  onChange: (p: Periodo) => void
}) {
  const hoje = dataLocal(new Date())
  const [aberto, setAberto] = useState(periodo.tipo === 'personalizado')
  const [de, setDe] = useState(
    periodo.tipo === 'personalizado'
      ? periodo.de
      : dataLocal(new Date(Date.now() - 30 * 24 * 60 * 60 * 1000)),
  )
  const [ate, setAte] = useState(periodo.tipo === 'personalizado' ? periodo.ate : hoje)
  const erro = aberto ? erroDoPersonalizado(de, ate, new Date()) : null

  return (
    <div className="periodo">
      <div className="row-tight">
        {ATALHOS.map((a) => (
          <button
            key={a.dias}
            className={`small ${periodo.tipo === 'atalho' && periodo.dias === a.dias ? 'primary' : ''}`}
            onClick={() => {
              setAberto(false)
              onChange({ tipo: 'atalho', dias: a.dias })
            }}
          >
            {a.rotulo}
          </button>
        ))}
        <button
          className={`small ${periodo.tipo === 'personalizado' ? 'primary' : ''}`}
          onClick={() => setAberto((v) => !v)}
          aria-expanded={aberto}
        >
          Personalizado
        </button>
      </div>
      {aberto && (
        <div className="periodo-datas">
          <label>
            De
            <input type="date" value={de} max={ate || hoje} onChange={(e) => setDe(e.target.value)} />
          </label>
          <label>
            Até
            <input type="date" value={ate} min={de} max={hoje} onChange={(e) => setAte(e.target.value)} />
          </label>
          <button
            className="small primary"
            disabled={erro !== null}
            onClick={() => onChange({ tipo: 'personalizado', de, ate })}
          >
            Aplicar
          </button>
          {erro && <span className="periodo-erro">{erro}</span>}
        </div>
      )}
    </div>
  )
}
