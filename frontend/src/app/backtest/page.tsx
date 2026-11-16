"use client";

/**
 * /backtest: run one strategy on one ticker and compare it with buy-and-hold.
 *
 * The engine is market/backtest.py: a decision at a close is filled at the
 * next open, with commission and slippage, on dividend-adjusted prices. The
 * equity curves are drawn with lightweight-charts (Apache-2.0; attribution
 * below the chart, as its licence asks).
 */

import { ColorType, createChart, LineSeries, type Time } from "lightweight-charts";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { api, type BacktestResult, type StrategyInfo } from "@/lib/api";
import { errorMessage, useAuth } from "@/lib/auth";

function isoDaysAgo(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

const pct = (x: number | null | undefined) =>
  x === null || x === undefined ? "–" : `${(x * 100).toFixed(2)}%`;
const num = (x: number | null | undefined, digits = 2) =>
  x === null || x === undefined ? "–" : x.toFixed(digits);
const money = (x: number | null | undefined) =>
  x === null || x === undefined
    ? "–"
    : x.toLocaleString(undefined, { maximumFractionDigits: 0 });

const STAT_ROWS: { key: string; label: string; format: (x: number | null) => string }[] = [
  { key: "total_return", label: "Total return", format: pct },
  { key: "cagr", label: "CAGR", format: pct },
  { key: "annualised_volatility", label: "Volatility (annual)", format: pct },
  { key: "sharpe_ratio", label: "Sharpe ratio", format: (x) => num(x) },
  { key: "max_drawdown", label: "Max drawdown", format: pct },
  { key: "trades", label: "Trades", format: (x) => num(x, 0) },
  { key: "win_rate", label: "Win rate", format: pct },
  { key: "average_trade_return", label: "Average trade", format: pct },
  { key: "exposure", label: "Time in market", format: pct },
  { key: "costs_paid", label: "Costs paid", format: money },
  { key: "final_equity", label: "Final equity", format: money },
];

function EquityChart({ result }: { result: BacktestResult }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!box.current) return;
    const chart = createChart(box.current, {
      height: 360,
      autoSize: true,
      layout: {
        attributionLogo: true,
        background: { type: ColorType.Solid, color: "#0a0a0a" },
        textColor: "#a3a3a3",
      },
      grid: { vertLines: { color: "#171717" }, horzLines: { color: "#171717" } },
    });
    const line = (values: number[], color: string) =>
      chart
        .addSeries(LineSeries, { color, lineWidth: 2, priceLineVisible: false })
        .setData(result.days.map((d, i) => ({ time: d as Time, value: values[i] })));
    line(result.benchmark_equity, "#737373");
    line(result.equity, "#38bdf8");
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [result]);
  return <div ref={box} style={{ height: 360 }} />;
}

export default function BacktestPage() {
  const router = useRouter();
  const { token, ready } = useAuth();
  const [catalogue, setCatalogue] = useState<StrategyInfo[]>([]);
  const [ticker, setTicker] = useState("NVDA");
  const [strategy, setStrategy] = useState("sma_cross");
  const [params, setParams] = useState<Record<string, number>>({});
  const [start, setStart] = useState(isoDaysAgo(365 * 5));
  const [end, setEnd] = useState(isoDaysAgo(0));
  const [costBps, setCostBps] = useState(5);
  const [slippageBps, setSlippageBps] = useState(5);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    if (ready && !token) router.replace("/login");
  }, [ready, token, router]);

  useEffect(() => {
    if (!token) return;
    api.strategies(token).then(setCatalogue).catch((e) => setError(errorMessage(e)));
  }, [token]);

  const chosen = catalogue.find((s) => s.key === strategy);

  async function run(event: React.FormEvent) {
    event.preventDefault();
    if (!token) return;
    setRunning(true);
    setError(null);
    try {
      setResult(await api.backtest(token, ticker, {
        strategy, params, start, end, cost_bps: costBps, slippage_bps: slippageBps,
      }));
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setRunning(false);
    }
  }

  if (!ready || !token) {
    return <p className="p-6 text-sm text-neutral-500">Loading…</p>;
  }

  const input =
    "mt-1 rounded-md border border-neutral-800 bg-neutral-900 px-2 py-1 text-sm text-neutral-100";
  return (
    <main className="mx-auto max-w-5xl px-4 py-6">
      <div className="mb-4 flex items-baseline justify-between">
        <h1 className="text-lg font-medium">Backtest a strategy</h1>
        <div className="flex gap-4 text-xs text-neutral-400">
          <Link href="/chart" className="underline">Price charts</Link>
          <Link href="/research" className="underline">Back to the chat</Link>
        </div>
      </div>

      <form onSubmit={run} className="mb-4 flex flex-wrap items-end gap-3 text-xs text-neutral-400">
        <label className="flex flex-col">
          Ticker
          <input value={ticker} onChange={(e) => setTicker(e.target.value.toUpperCase())}
            className={`${input} w-24 uppercase`} />
        </label>
        <label className="flex flex-col">
          Strategy
          <select value={strategy}
            onChange={(e) => {
              setStrategy(e.target.value);
              setParams({});
            }}
            className={input}>
            {catalogue.map((s) => (
              <option key={s.key} value={s.key}>{s.name}</option>
            ))}
          </select>
        </label>
        {chosen &&
          Object.entries(chosen.params).map(([name, value]) => (
            <label key={name} className="flex flex-col">
              {name}
              <input type="number" step="any" className={`${input} w-20`}
                value={params[name] ?? value}
                onChange={(e) => setParams({ ...params, [name]: Number(e.target.value) })} />
            </label>
          ))}
        <label className="flex flex-col">
          From
          <input type="date" value={start} onChange={(e) => setStart(e.target.value)} className={input} />
        </label>
        <label className="flex flex-col">
          To
          <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} className={input} />
        </label>
        <label className="flex flex-col">
          Commission (bps)
          <input type="number" min={0} step="any" value={costBps}
            onChange={(e) => setCostBps(Number(e.target.value))} className={`${input} w-20`} />
        </label>
        <label className="flex flex-col">
          Slippage (bps)
          <input type="number" min={0} step="any" value={slippageBps}
            onChange={(e) => setSlippageBps(Number(e.target.value))} className={`${input} w-20`} />
        </label>
        <button type="submit" disabled={running}
          className="rounded-md bg-neutral-100 px-3 py-1.5 text-sm font-medium text-neutral-900 disabled:opacity-50">
          {running ? "Running…" : "Run"}
        </button>
      </form>
      {chosen && <p className="mb-4 text-xs text-neutral-500">{chosen.description}</p>}
      {error && <p className="mb-4 text-xs text-red-300">{error}</p>}

      {result && (
        <>
          <p className="mb-2 text-xs text-neutral-400">
            {result.ticker}, {result.start} to {result.end}.{" "}
            <span className="text-sky-400">Strategy</span> against{" "}
            <span className="text-neutral-400">buy-and-hold</span>, from{" "}
            {money(result.initial_cash)}. {result.execution}; {result.cost_bps} bps commission and{" "}
            {result.slippage_bps} bps slippage per side.
          </p>
          <EquityChart result={result} />
          <p className="mt-2 text-[10px] text-neutral-600">
            Charts: TradingView Lightweight Charts™, Copyright (c) 2025 TradingView, Inc.,{" "}
            <a href="https://www.tradingview.com/" className="underline" target="_blank" rel="noreferrer">
              tradingview.com
            </a>
            . Past performance in a backtest says nothing reliable about the future; this is
            not investment advice.
          </p>

          <table className="mt-6 w-full max-w-lg text-xs">
            <thead>
              <tr className="text-left text-neutral-500">
                <th className="py-1 font-normal"></th>
                <th className="py-1 font-normal">Strategy</th>
                <th className="py-1 font-normal">Buy and hold</th>
              </tr>
            </thead>
            <tbody>
              {STAT_ROWS.map((row) => (
                <tr key={row.key} className="border-t border-neutral-900">
                  <td className="py-1 text-neutral-400">{row.label}</td>
                  <td className="py-1 text-neutral-100">{row.format(result.stats[row.key])}</td>
                  <td className="py-1 text-neutral-400">{row.format(result.benchmark_stats[row.key])}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h2 className="mt-8 mb-2 text-sm font-medium">Trades ({result.trades.length})</h2>
          <div className="max-h-96 overflow-y-auto">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-neutral-950 text-left text-neutral-500">
                <tr>
                  <th className="py-1 font-normal">Side</th>
                  <th className="py-1 font-normal">Entry</th>
                  <th className="py-1 font-normal">Fill</th>
                  <th className="py-1 font-normal">Exit</th>
                  <th className="py-1 font-normal">Fill</th>
                  <th className="py-1 font-normal">Days</th>
                  <th className="py-1 font-normal">Return, net</th>
                </tr>
              </thead>
              <tbody>
                {result.trades.map((t, i) => (
                  <tr key={i} className="border-t border-neutral-900">
                    <td className="py-1">{t.side}{t.status === "open" ? " (open)" : ""}</td>
                    <td className="py-1">{t.entry_day}</td>
                    <td className="py-1">{num(t.entry_price)}</td>
                    <td className="py-1">{t.exit_day ?? "–"}</td>
                    <td className="py-1">{num(t.exit_price)}</td>
                    <td className="py-1">{t.bars_held}</td>
                    <td className={`py-1 ${(t.net_return ?? 0) >= 0 ? "text-green-400" : "text-red-400"}`}>
                      {pct(t.net_return)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </main>
  );
}
