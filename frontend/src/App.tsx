import { NavLink, Navigate, Route, Routes } from 'react-router-dom'

import { useLiveEvents } from './hooks/useLiveEvents'
import Agents from './pages/Agents'
import Backtest from './pages/Backtest'
import Configuration from './pages/Configuration'
import Dashboard from './pages/Dashboard'
import ManualTrade from './pages/ManualTrade'
import Notifications from './pages/Notifications'
import RiskSettings from './pages/RiskSettings'
import Trades from './pages/Trades'

/**
 * Icones do menu, em SVG proprio.
 *
 * Eram glifos Unicode geometricos — `◧ ☰ ✎ ⛨ ⚙ ◈ ✉ ◔`. Tres problemas com isso,
 * e nenhum aparece no `tsc`: o desenho depende da fonte instalada na maquina
 * (`⛨` cai para caixa vazia em Windows sem Segoe UI Symbol), o peso do traco nao
 * combina com o resto da interface porque vem da fonte de texto, e o alinhamento
 * vertical varia glifo a glifo. Nenhuma referencia de mercado usa dingbats de
 * fonte no menu.
 *
 * Custo: ~1 kB de path, nenhuma dependencia. `currentColor` faz o icone seguir a
 * cor do item, inclusive no estado ativo e nos dois temas.
 */
const traco = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.6,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
}

const ICONS: Record<string, JSX.Element> = {
  dashboard: (
    <>
      <rect x="3" y="3" width="7.5" height="7.5" rx="1.5" {...traco} />
      <rect x="13.5" y="3" width="7.5" height="7.5" rx="1.5" {...traco} />
      <rect x="3" y="13.5" width="7.5" height="7.5" rx="1.5" {...traco} />
      <rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.5" {...traco} />
    </>
  ),
  trades: <path d="M4 6.5h16M4 12h16M4 17.5h10" {...traco} />,
  manual: (
    <>
      <path d="M4.5 19.5h4L19 9l-4-4L4.5 15.5z" {...traco} />
      <path d="M14 6l4 4" {...traco} />
    </>
  ),
  risk: (
    <>
      <path d="M12 3l7.5 3v5.5c0 4.6-3.1 8.2-7.5 10.2-4.4-2-7.5-5.6-7.5-10.2V6z" {...traco} />
      <path d="M12 9v4M12 16.2v.1" {...traco} />
    </>
  ),
  config: (
    <>
      <path d="M4 7h5.5M13.5 7H20M4 12h9M17 12h3M4 17h3M10.5 17H20" {...traco} />
      <circle cx="11.5" cy="7" r="2" {...traco} />
      <circle cx="15" cy="12" r="2" {...traco} />
      <circle cx="8.5" cy="17" r="2" {...traco} />
    </>
  ),
  agents: (
    <>
      <rect x="8.5" y="8.5" width="7" height="7" rx="1.5" {...traco} />
      <path d="M12 3v5.5M12 15.5V21M3 12h5.5M15.5 12H21" {...traco} />
    </>
  ),
  notifications: (
    <>
      <path d="M18 16.5v-5a6 6 0 00-12 0v5l-1.5 2h15z" {...traco} />
      <path d="M10 19.5a2 2 0 004 0" {...traco} />
    </>
  ),
  backtest: (
    <>
      <path d="M4 4v16h16" {...traco} />
      <path d="M7.5 15.5l3.5-4.5 3 2.5 4.5-6" {...traco} />
    </>
  ),
}

const NAV = [
  { to: '/dashboard', label: 'Dashboard', icon: 'dashboard' },
  { to: '/trades', label: 'Negociações', icon: 'trades' },
  { to: '/manual', label: 'Lançar manual', icon: 'manual' },
  { to: '/risk', label: 'Risco', icon: 'risk' },
  { to: '/config', label: 'Configurações', icon: 'config' },
  { to: '/agents', label: 'Agentes', icon: 'agents' },
  { to: '/notifications', label: 'Notificações', icon: 'notifications' },
  { to: '/backtest', label: 'Backtest', icon: 'backtest' },
]

export default function App() {
  const { connected, feed } = useLiveEvents()

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <strong>Crypto Traders</strong>
          <span>agentes autônomos</span>
        </div>

        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}
          >
            <svg className="nav-icon" viewBox="0 0 24 24" aria-hidden="true">
              {ICONS[item.icon]}
            </svg>
            {item.label}
          </NavLink>
        ))}

        <div style={{ marginTop: 'auto', padding: '14px 10px 0' }}>
          <div className="row-tight" style={{ fontSize: 11, color: 'var(--text-faint)' }}>
            <span className={`dot ${connected ? 'dot-live' : 'dot-off'}`} />
            {connected ? 'tempo real ativo' : 'reconectando…'}
          </div>
        </div>
      </aside>

      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<Dashboard feed={feed} />} />
          <Route path="/trades" element={<Trades />} />
          <Route path="/manual" element={<ManualTrade />} />
          <Route path="/risk" element={<RiskSettings />} />
          <Route path="/config" element={<Configuration />} />
          <Route path="/agents" element={<Agents />} />
          <Route path="/notifications" element={<Notifications />} />
          <Route path="/backtest" element={<Backtest />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </main>
    </div>
  )
}
