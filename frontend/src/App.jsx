/**
 * PolyBot Dashboard — Root Application Component
 *
 * Manages:
 *  - WebSocket connection for real-time updates
 *  - Global state (traders, positions, PnL)
 *  - Navigation between Dashboard, Positions, and Alerts views
 */
import { useState, useEffect, useCallback, useRef } from 'react'
import Dashboard from './components/Dashboard'
import Positions from './components/Positions'
import Alerts from './components/Alerts'

const WS_URL = import.meta.env.VITE_WS_URL || 'ws://localhost:8000/ws'
const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000'

const NAV_ITEMS = ['Dashboard', 'Positions', 'Alerts']

export default function App() {
  const [activeTab, setActiveTab] = useState('Dashboard')
  const [traders, setTraders]     = useState([])
  const [pnl, setPnl]             = useState({})
  const [positions, setPositions] = useState([])
  const [sizing, setSizing]       = useState([])
  const [alerts, setAlerts]       = useState([])
  const [wsStatus, setWsStatus]   = useState('connecting')
  const wsRef = useRef(null)

  /* ------------------------------------------------------------------ */
  /* Data fetching helpers                                                */
  /* ------------------------------------------------------------------ */
  const fetchAll = useCallback(async () => {
    try {
      const [tradersRes, pnlRes, posRes, sizingRes] = await Promise.all([
        fetch(`${API_BASE}/api/traders`),
        fetch(`${API_BASE}/api/pnl`),
        fetch(`${API_BASE}/api/positions`),
        fetch(`${API_BASE}/api/sizing`),
      ])
      if (tradersRes.ok) setTraders((await tradersRes.json()).traders ?? [])
      if (pnlRes.ok)     setPnl(await pnlRes.json())
      if (posRes.ok)     setPositions((await posRes.json()).open_positions ?? [])
      if (sizingRes.ok)  setSizing((await sizingRes.json()).sizing ?? [])
    } catch (err) {
      console.error('Fetch error:', err)
    }
  }, [])

  const fetchAlerts = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/alerts`)
      if (res.ok) setAlerts((await res.json()).log ?? [])
    } catch (err) {
      console.error('Alerts fetch error:', err)
    }
  }, [])

  /* ------------------------------------------------------------------ */
  /* WebSocket                                                            */
  /* ------------------------------------------------------------------ */
  useEffect(() => {
    let ws
    let reconnectTimer

    const connect = () => {
      ws = new WebSocket(WS_URL)
      wsRef.current = ws

      ws.onopen = () => {
        setWsStatus('connected')
        console.log('WebSocket connected')
      }

      ws.onmessage = (evt) => {
        try {
          const msg = JSON.parse(evt.data)
          if (msg.type === 'discovery_update' || msg.type === 'init') {
            fetchAll()
          }
          if (msg.type === 'trade_opened' || msg.type === 'trade_closed') {
            fetchAll()
            fetchAlerts()
          }
        } catch (e) {
          console.warn('WS message parse error:', e)
        }
      }

      ws.onerror = () => setWsStatus('error')

      ws.onclose = () => {
        setWsStatus('disconnected')
        reconnectTimer = setTimeout(connect, 3000)
      }
    }

    connect()
    fetchAll()
    fetchAlerts()

    const pollTimer = setInterval(fetchAll, 30_000)

    return () => {
      clearTimeout(reconnectTimer)
      clearInterval(pollTimer)
      ws?.close()
    }
  }, [fetchAll, fetchAlerts])

  /* ------------------------------------------------------------------ */
  /* Render                                                               */
  /* ------------------------------------------------------------------ */
  const wsColor = wsStatus === 'connected'
    ? 'text-green-400'
    : wsStatus === 'connecting'
    ? 'text-yellow-400'
    : 'text-red-400'

  return (
    <div className="min-h-screen flex flex-col">
      {/* ---- Header ---- */}
      <header className="bg-gray-900 border-b border-gray-800 px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className="text-2xl font-bold text-green-400">🤖 PolyBot</span>
          <span className="text-xs text-gray-400 hidden sm:block">Polymarket Copy-Trading System</span>
        </div>
        <div className="flex items-center gap-6">
          {NAV_ITEMS.map((tab) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`text-sm font-medium transition-colors ${
                activeTab === tab
                  ? 'text-green-400 border-b-2 border-green-400 pb-1'
                  : 'text-gray-400 hover:text-gray-200'
              }`}
            >
              {tab}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2 text-xs">
          <span className={`w-2 h-2 rounded-full inline-block ${wsColor.replace('text-', 'bg-')}`} />
          <span className={wsColor}>{wsStatus}</span>
        </div>
      </header>

      {/* ---- Content ---- */}
      <main className="flex-1 p-6">
        {activeTab === 'Dashboard' && (
          <Dashboard traders={traders} pnl={pnl} sizing={sizing} />
        )}
        {activeTab === 'Positions' && (
          <Positions positions={positions} onRefresh={fetchAll} apiBase={API_BASE} />
        )}
        {activeTab === 'Alerts' && (
          <Alerts alerts={alerts} onRefresh={fetchAlerts} />
        )}
      </main>

      {/* ---- Footer ---- */}
      <footer className="text-center text-xs text-gray-600 py-3 border-t border-gray-800">
        PolyBot · Polymarket Copy-Trading System · {new Date().getFullYear()}
      </footer>
    </div>
  )
}
