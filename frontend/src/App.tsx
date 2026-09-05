import { NavLink, Navigate, Route, Routes } from 'react-router-dom'

import { useLiveEvents } from './hooks/useLiveEvents'
import Agents from './pages/Agents'
import Backtest from './pages/Backtest'
import Dashboard from './pages/Dashboard'
import ManualTrade from './pages/ManualTrade'
import RiskSettings from './pages/RiskSettings'
import Trades from './pages/Trades'

const NAV = [
  { to: '/dashboard', label: 'Dashboard', icon: '◧' },
  { to: '/trades', label: 'Negociações', icon: '☰' },
  { to: '/manual', label: 'Lançar manual', icon: '✎' },
  { to: '/risk', label: 'Risco', icon: '⛨' },
  { to: '/agents', label: 'Agentes', icon: '◈' },
  { to: '/backtest', label: 'Backtest', icon: '◔' },
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
            <span className="nav-icon">{item.icon}</span>
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
          <Route path="/agents" element={<Agents />} />
          <Route path="/backtest" element={<Backtest />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </main>
    </div>
  )
}
