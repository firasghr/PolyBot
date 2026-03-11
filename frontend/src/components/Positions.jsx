/**
 * Positions Component
 *
 * Displays all open paper-trading positions with entry price, size,
 * expected EV, and a manual close form for testing.
 */
import { useState } from 'react'

const fmtUsd = (n) =>
  typeof n === 'number'
    ? new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n)
    : '—'

const fmtTs = (ts) =>
  ts ? new Date(ts * 1000).toLocaleString() : '—'

export default function Positions({ positions = [], onRefresh, apiBase = '' }) {
  const [closeId, setCloseId]     = useState('')
  const [exitPrice, setExitPrice] = useState('')
  const [status, setStatus]       = useState('')

  const handleClose = async () => {
    if (!closeId || !exitPrice) return
    try {
      const res = await fetch(`${apiBase}/api/trades/close`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ trade_id: closeId, exit_price: parseFloat(exitPrice) }),
      })
      const data = await res.json()
      setStatus(
        res.ok
          ? `✅ Closed ${closeId}: ${data.outcome?.toUpperCase()}  PnL ${fmtUsd(data.realised_pnl_usdc)}`
          : `❌ Error: ${JSON.stringify(data)}`,
      )
      setCloseId('')
      setExitPrice('')
      onRefresh?.()
    } catch (err) {
      setStatus(`❌ ${err.message}`)
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold text-gray-100">Open Positions</h1>
        <button
          onClick={onRefresh}
          className="px-3 py-1 text-xs bg-gray-700 hover:bg-gray-600 rounded-lg transition-colors"
        >
          ↻ Refresh
        </button>
      </div>

      {positions.length === 0 ? (
        <div className="bg-gray-800 rounded-xl p-8 text-center text-gray-500 italic">
          No open positions
        </div>
      ) : (
        <div className="bg-gray-800 rounded-xl overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-gray-400 border-b border-gray-700 bg-gray-900/50">
                <th className="px-4 py-3">Trade ID</th>
                <th className="px-4 py-3">Wallet</th>
                <th className="px-4 py-3">Market</th>
                <th className="px-4 py-3">Side</th>
                <th className="px-4 py-3">Entry</th>
                <th className="px-4 py-3">Size</th>
                <th className="px-4 py-3">EV</th>
                <th className="px-4 py-3">Opened</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr
                  key={p.trade_id}
                  className="border-b border-gray-700/50 hover:bg-gray-700/30 transition-colors cursor-pointer"
                  onClick={() => setCloseId(p.trade_id)}
                >
                  <td className="px-4 py-3 font-mono text-xs text-gray-400">
                    {p.trade_id.slice(0, 8)}…
                  </td>
                  <td className="px-4 py-3 font-mono text-xs">
                    {p.wallet?.slice(0, 6)}…{p.wallet?.slice(-4)}
                  </td>
                  <td className="px-4 py-3 text-gray-300 max-w-[200px] truncate" title={p.market}>
                    {p.market}
                  </td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-0.5 rounded text-xs font-bold ${p.side === 'BUY' ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}`}>
                      {p.side}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-gray-300">{(p.entry_price ?? 0).toFixed(4)}</td>
                  <td className="px-4 py-3 text-gray-300">{fmtUsd(p.size_usdc)}</td>
                  <td className={`px-4 py-3 font-semibold ${(p.expected_ev_usdc ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                    {fmtUsd(p.expected_ev_usdc)}
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-500">{fmtTs(p.timestamp)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* ---- Manual close form ---- */}
      <div className="bg-gray-800 rounded-xl p-4 space-y-3">
        <h2 className="text-sm font-semibold text-gray-300">Close Position (click row to pre-fill)</h2>
        <div className="flex gap-3 flex-wrap">
          <input
            type="text"
            placeholder="Trade ID"
            value={closeId}
            onChange={(e) => setCloseId(e.target.value)}
            className="flex-1 min-w-[200px] bg-gray-700 border border-gray-600 rounded-lg px-3 py-2 text-sm text-gray-100 placeholder-gray-500 focus:outline-none focus:border-green-500"
          />
          <input
            type="number"
            step="0.0001"
            placeholder="Exit price (0–1)"
            value={exitPrice}
            onChange={(e) => setExitPrice(e.target.value)}
            className="w-44 bg-gray-700 border border-gray-600 rounded-lg px-3 py-2 text-sm text-gray-100 placeholder-gray-500 focus:outline-none focus:border-green-500"
          />
          <button
            onClick={handleClose}
            disabled={!closeId || !exitPrice}
            className="px-4 py-2 bg-red-700 hover:bg-red-600 disabled:opacity-40 text-white rounded-lg text-sm font-medium transition-colors"
          >
            Close Trade
          </button>
        </div>
        {status && (
          <p className="text-xs text-gray-300 bg-gray-700 rounded-lg px-3 py-2">{status}</p>
        )}
      </div>
    </div>
  )
}
