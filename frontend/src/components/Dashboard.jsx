/**
 * Dashboard Component
 *
 * Displays:
 *  - PnL summary cards (realised, win rate, drawdown, Sharpe)
 *  - Top traders table with key metrics
 *  - Position sizing recommendations per wallet
 *  - Equity-curve sparkline chart
 */
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'

const MetricCard = ({ label, value, sub, color = 'text-white' }) => (
  <div className="bg-gray-800 rounded-xl p-4 flex flex-col gap-1">
    <span className="text-xs text-gray-400 uppercase tracking-wide">{label}</span>
    <span className={`text-2xl font-bold ${color}`}>{value}</span>
    {sub && <span className="text-xs text-gray-500">{sub}</span>}
  </div>
)

const fmt = (n, dec = 2) =>
  typeof n === 'number' ? n.toFixed(dec) : '—'

const fmtUsd = (n) =>
  typeof n === 'number'
    ? new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(n)
    : '—'

export default function Dashboard({ traders = [], pnl = {}, sizing = [] }) {
  /* ---- PnL metrics ---- */
  const pnlColor  = (pnl.realised_pnl_usdc ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'
  const ddColor   = (pnl.max_drawdown_pct  ?? 0) <= 5  ? 'text-green-400' : 'text-yellow-400'
  const winColor  = (pnl.win_rate          ?? 0) >= 0.6 ? 'text-green-400' : 'text-yellow-400'

  /* ---- Build a simple sparkline dataset from sizing array ---- */
  const sparkData = sizing.map((s, i) => ({
    name: `#${i + 1}`,
    size: +(s.effective_size_usdc ?? 0).toFixed(2),
  }))

  return (
    <div className="space-y-6">
      {/* ---- Metric cards ---- */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <MetricCard
          label="Realised PnL"
          value={fmtUsd(pnl.realised_pnl_usdc)}
          sub={`Unrealised: ${fmtUsd(pnl.unrealised_pnl_usdc)}`}
          color={pnlColor}
        />
        <MetricCard
          label="Win Rate"
          value={`${fmt(pnl.win_rate ? pnl.win_rate * 100 : undefined)}%`}
          sub={`${pnl.total_trades ?? 0} total trades`}
          color={winColor}
        />
        <MetricCard
          label="Max Drawdown"
          value={`${fmt(pnl.max_drawdown_pct)}%`}
          sub="Peak-to-trough"
          color={ddColor}
        />
        <MetricCard
          label="Sharpe Ratio"
          value={fmt(pnl.sharpe_ratio)}
          sub="Historical"
          color="text-blue-300"
        />
      </div>

      {/* ---- Sizing chart ---- */}
      {sparkData.length > 0 && (
        <div className="bg-gray-800 rounded-xl p-4">
          <h2 className="text-sm font-semibold text-gray-300 mb-3">
            Recommended Position Sizes (USDC) · {sizing[0]?.kelly_mode ?? ''} Kelly
          </h2>
          <ResponsiveContainer width="100%" height={160}>
            <AreaChart data={sparkData}>
              <defs>
                <linearGradient id="sizeGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor="#22c55e" stopOpacity={0.4} />
                  <stop offset="95%" stopColor="#22c55e" stopOpacity={0.0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
              <XAxis dataKey="name" tick={{ fill: '#9ca3af', fontSize: 11 }} />
              <YAxis tick={{ fill: '#9ca3af', fontSize: 11 }} tickFormatter={(v) => `$${v}`} />
              <Tooltip
                contentStyle={{ background: '#1f2937', border: 'none', borderRadius: 8 }}
                formatter={(v) => [`$${v}`, 'Size']}
              />
              <Area
                type="monotone"
                dataKey="size"
                stroke="#22c55e"
                fill="url(#sizeGrad)"
                strokeWidth={2}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* ---- Top traders table ---- */}
      <div className="bg-gray-800 rounded-xl p-4">
        <h2 className="text-sm font-semibold text-gray-300 mb-3">
          Top {traders.length} Discovered Traders
        </h2>
        {traders.length === 0 ? (
          <p className="text-gray-500 text-sm italic">
            No traders loaded yet. Discovery runs every 5 minutes.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-400 border-b border-gray-700">
                  <th className="pb-2 pr-4">Wallet</th>
                  <th className="pb-2 pr-4">Win Rate</th>
                  <th className="pb-2 pr-4">Trades</th>
                  <th className="pb-2 pr-4">Avg Size</th>
                  <th className="pb-2 pr-4">Sharpe</th>
                  <th className="pb-2">Focus</th>
                </tr>
              </thead>
              <tbody>
                {traders.map((t) => (
                  <tr key={t.wallet} className="border-b border-gray-700/50 hover:bg-gray-700/30 transition-colors">
                    <td className="py-2 pr-4 font-mono text-xs text-gray-300">
                      {t.wallet.slice(0, 6)}…{t.wallet.slice(-4)}
                    </td>
                    <td className={`py-2 pr-4 font-semibold ${t.win_rate >= 0.7 ? 'text-green-400' : 'text-yellow-400'}`}>
                      {fmt(t.win_rate * 100)}%
                    </td>
                    <td className="py-2 pr-4 text-gray-300">{t.trade_count}</td>
                    <td className="py-2 pr-4 text-gray-300">{fmtUsd(t.avg_position_size_usdc)}</td>
                    <td className="py-2 pr-4 text-blue-300">{fmt(t.sharpe_ratio)}</td>
                    <td className="py-2">
                      <span className="px-2 py-0.5 rounded-full text-xs bg-gray-700 text-gray-300 capitalize">
                        {t.market_focus}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ---- Sizing table ---- */}
      {sizing.length > 0 && (
        <div className="bg-gray-800 rounded-xl p-4">
          <h2 className="text-sm font-semibold text-gray-300 mb-3">
            Risk-Adjusted Position Sizes
          </h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-400 border-b border-gray-700">
                  <th className="pb-2 pr-4">Wallet</th>
                  <th className="pb-2 pr-4">Kelly f</th>
                  <th className="pb-2 pr-4">Raw Kelly</th>
                  <th className="pb-2 pr-4">Risk Capped</th>
                  <th className="pb-2 pr-4">Slippage</th>
                  <th className="pb-2">Final Size</th>
                </tr>
              </thead>
              <tbody>
                {sizing.map((s) => (
                  <tr key={s.wallet} className="border-b border-gray-700/50">
                    <td className="py-2 pr-4 font-mono text-xs text-gray-300">
                      {s.wallet.slice(0, 6)}…{s.wallet.slice(-4)}
                    </td>
                    <td className="py-2 pr-4 text-gray-300">{fmt(s.kelly_fraction * 100)}%</td>
                    <td className="py-2 pr-4 text-gray-300">{fmtUsd(s.raw_kelly_size_usdc)}</td>
                    <td className="py-2 pr-4 text-yellow-400">{fmtUsd(s.risk_capped_size_usdc)}</td>
                    <td className="py-2 pr-4 text-gray-400">{fmt(s.slippage_pct)}%</td>
                    <td className="py-2 font-bold text-green-400">{fmtUsd(s.effective_size_usdc)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
