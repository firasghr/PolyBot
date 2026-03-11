/**
 * Alerts Component
 *
 * Displays the trade log / alert stream from the backend.
 * All entries are shown newest-first.
 */

const fmtTs = (ts) =>
  ts ? new Date(ts * 1000).toLocaleString() : '—'

const statusColor = (status) => {
  if (!status) return 'text-gray-400'
  if (status === 'opened') return 'text-blue-400'
  if (status === 'closed') return 'text-green-400'
  if (status === 'failed') return 'text-red-400'
  return 'text-yellow-400'
}

export default function Alerts({ alerts = [], onRefresh }) {
  const sorted = [...alerts].reverse()

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold text-gray-100">Trade Alerts & Log</h1>
        <button
          onClick={onRefresh}
          className="px-3 py-1 text-xs bg-gray-700 hover:bg-gray-600 rounded-lg transition-colors"
        >
          ↻ Refresh
        </button>
      </div>

      {sorted.length === 0 ? (
        <div className="bg-gray-800 rounded-xl p-8 text-center text-gray-500 italic">
          No alerts yet
        </div>
      ) : (
        <div className="space-y-2">
          {sorted.map((alert, idx) => (
            <div
              key={idx}
              className="bg-gray-800 rounded-xl px-4 py-3 flex flex-wrap items-center gap-3 text-sm"
            >
              <span className="text-xs text-gray-500 shrink-0 w-36">{fmtTs(alert.timestamp)}</span>
              <span className={`font-mono text-xs shrink-0 ${statusColor(alert.status)}`}>
                [{alert.status?.toUpperCase() ?? 'EVENT'}]
              </span>
              <span className="font-mono text-xs text-gray-400 shrink-0">
                {alert.trade_id?.slice(0, 8) ?? ''}
              </span>
              <span className="text-gray-300 flex-1 truncate" title={alert.market ?? ''}>
                {alert.market ?? alert.wallet ?? JSON.stringify(alert)}
              </span>
              {alert.outcome && (
                <span className={`px-2 py-0.5 rounded text-xs font-bold ${alert.outcome === 'win' ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}`}>
                  {alert.outcome.toUpperCase()}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
