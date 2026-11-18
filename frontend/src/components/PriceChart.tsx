"use client";

/**
 * A price chart: candlesticks, with volume and indicators in panes below.
 *
 * Drawn with TradingView's lightweight-charts (Apache-2.0). Its licence asks
 * for TradingView's attribution notice and a link to tradingview.com on any
 * page that shows a chart: the chart's own attribution logo is left on, and
 * the notice is printed under every chart.
 *
 * Data comes from GET /market/{ticker}/chart, the same endpoint for the chart
 * page and for a chart the agent returned in the chat.
 */

import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  type IChartApi,
  type Time,
} from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import { api, type ChartData, type ChartSpec } from "@/lib/api";
import { errorMessage } from "@/lib/auth";

const COLORS: Record<string, string> = {
  sma20: "#f59e0b",
  sma50: "#38bdf8",
  sma200: "#e879f9",
  ema12: "#a3e635",
  ema26: "#fb7185",
  bb_middle: "#94a3b8",
  bb_upper: "#64748b",
  bb_lower: "#64748b",
  rsi14: "#c084fc",
  macd: "#38bdf8",
  macd_signal: "#f59e0b",
  atr14: "#f472b6",
  obv: "#2dd4bf",
  volume_sma20: "#fbbf24",
};

// Which pane each series below the price is drawn in, in order.
const LOWER_PANES: { key: string; series: string[] }[] = [
  { key: "volume", series: ["volume", "volume_sma20"] },
  { key: "rsi", series: ["rsi14"] },
  { key: "macd", series: ["macd_hist", "macd", "macd_signal"] },
  { key: "atr", series: ["atr14"] },
  { key: "obv", series: ["obv"] },
];
const PRICE_OVERLAYS = ["sma20", "sma50", "sma200", "ema12", "ema26", "bb_upper", "bb_middle", "bb_lower"];

type Point = { time: Time; value: number };

/** A day the signal switched: "in" (went long) or "out" (went flat). */
export type SignalMarker = { day: string; action: "in" | "out" };

// The design tokens (globals.css), repeated here because the chart draws on a
// canvas and cannot read CSS variables. Green and red: up/down bars, and the
// signal going in/out.
const THEME = {
  bg: "#11161d", text: "#a3afbf", grid: "#1b2230", border: "#253041",
  up: "#3fd17f", down: "#f26b6b", upDim: "#1d4d33", downDim: "#5a2a2a",
};

function points(data: ChartData, key: string): Point[] {
  return (data.series[key] ?? []).map((p) => ({ time: p.time as Time, value: p.value }));
}

/** The first day to show when only the last `months` of the data are wanted:
 * the bar `months` calendar months before the last bar, or the first bar. */
export function visibleFrom(times: string[], months: number): string | null {
  if (!times.length) return null;
  const last = new Date(`${times[times.length - 1]}T00:00:00Z`);
  last.setUTCMonth(last.getUTCMonth() - months);
  const cutoff = last.toISOString().slice(0, 10);
  return times.find((t) => t >= cutoff) ?? times[0];
}

function draw(container: HTMLElement, data: ChartData, height: number,
              markers: SignalMarker[] = [], visibleMonths?: number): IChartApi {
  const chart = createChart(container, {
    height,
    autoSize: true,
    layout: {
      // TradingView's licence: the logo links to tradingview.com. Left on.
      attributionLogo: true,
      background: { type: ColorType.Solid, color: THEME.bg },
      textColor: THEME.text,
      panes: { separatorColor: THEME.border },
    },
    grid: { vertLines: { color: THEME.grid }, horzLines: { color: THEME.grid } },
    // The bars always reach both edges: no scrolling or zooming out into empty
    // space, and a resize keeps the same dates in view instead of leaving the
    // new width blank.
    timeScale: {
      borderColor: THEME.border, rightOffset: 0,
      fixLeftEdge: true, fixRightEdge: true, lockVisibleTimeRangeOnResize: true,
    },
    rightPriceScale: { borderColor: THEME.border },
  });

  const candles = chart.addSeries(CandlestickSeries, {
    upColor: THEME.up, downColor: THEME.down, borderVisible: false,
    wickUpColor: THEME.up, wickDownColor: THEME.down,
  });
  candles.setData(data.bars.map((b) => ({
    time: b.time as Time, open: b.open, high: b.high, low: b.low, close: b.close,
  })));
  const shown = new Set(data.bars.map((b) => b.time));
  if (markers.length) {
    createSeriesMarkers(candles, markers
      .filter((m) => shown.has(m.day))
      .map((m) => ({
        time: m.day as Time,
        position: m.action === "in" ? ("belowBar" as const) : ("aboveBar" as const),
        shape: m.action === "in" ? ("arrowUp" as const) : ("arrowDown" as const),
        color: m.action === "in" ? THEME.up : THEME.down,
        text: m.action === "in" ? "In" : "Out",
      })));
  }

  for (const key of PRICE_OVERLAYS) {
    if (!data.series[key]) continue;
    chart.addSeries(LineSeries, {
      color: COLORS[key], lineWidth: key.startsWith("bb_") ? 1 : 2,
      priceLineVisible: false, lastValueVisible: false,
    }).setData(points(data, key));
  }

  let pane = 0;
  for (const lower of LOWER_PANES) {
    if (!lower.series.some((k) => data.series[k])) continue;
    pane += 1;
    for (const key of lower.series) {
      if (!data.series[key]) continue;
      if (key === "volume") {
        const up = new Map(data.bars.map((b) => [b.time, b.close >= b.open]));
        chart.addSeries(HistogramSeries, { priceFormat: { type: "volume" } }, pane).setData(
          points(data, key).map((p) => ({
            ...p, color: up.get(p.time as string) ? THEME.upDim : THEME.downDim,
          })),
        );
      } else if (key === "macd_hist") {
        chart.addSeries(HistogramSeries, {}, pane).setData(
          points(data, key).map((p) => ({ ...p, color: p.value >= 0 ? THEME.upDim : THEME.downDim })),
        );
      } else {
        chart.addSeries(LineSeries, {
          color: COLORS[key], lineWidth: 1, priceLineVisible: false,
          ...(key === "obv" || key === "volume_sma20" ? { priceFormat: { type: "volume" as const } } : {}),
        }, pane).setData(points(data, key));
      }
    }
  }
  const panes = chart.panes();
  panes.forEach((p, i) => p.setStretchFactor(i === 0 ? 3 : 1));
  const times = data.bars.map((b) => b.time);
  const from = visibleMonths ? visibleFrom(times, visibleMonths) : null;
  if (from && from !== times[0]) {
    chart.timeScale().setVisibleRange({ from: from as Time, to: times[times.length - 1] as Time });
  } else {
    chart.timeScale().fitContent();
  }
  return chart;
}

export function chartHref(spec: ChartSpec): string {
  const query = new URLSearchParams({
    ticker: spec.ticker, start: spec.start, end: spec.end,
    indicators: spec.indicators.join(","),
  });
  return `/chart?${query.toString()}`;
}

const TABLE_DAYS = 20;

function fmt(n: number | undefined, digits = 2): string {
  return n === undefined || !Number.isFinite(n) ? "–" : n.toLocaleString("en-US", { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

/** The chart in words and as a table: the text alternative to the canvas. */
function ChartText({ data }: { data: ChartData }) {
  const bars = data.bars;
  const first = bars[0];
  const last = bars[bars.length - 1];
  const change = first && last ? (last.close / first.close - 1) * 100 : null;
  const keys = Object.keys(data.series).filter((k) => k !== "volume");
  const latest = Object.fromEntries(keys.map((k) => [k, data.series[k]?.at(-1)?.value]));
  const byDay = Object.fromEntries(keys.map((k) => [k, new Map(data.series[k].map((p) => [p.time, p.value]))]));
  const label = (k: string) => data.indicators[k]?.label ?? k;
  const recent = bars.slice(-TABLE_DAYS).reverse();
  return (
    <div className="mt-2 text-xs text-fg-muted">
      <p className="sr-only">
        {data.ticker} daily candles from {first?.time} to {last?.time}: close {fmt(first?.close)} then {fmt(last?.close)}
        {change === null ? "" : `, a change of ${change >= 0 ? "+" : ""}${change.toFixed(2)}%`}
        {keys.length ? `. Latest ${keys.map((k) => `${label(k)} ${fmt(latest[k])}`).join(", ")}.` : "."}
      </p>
      <details>
        <summary className="cursor-pointer text-fg-muted hover:text-fg">Show the last {recent.length} days as a table</summary>
        <div className="mt-2 max-h-72 overflow-auto rounded-md border border-border" tabIndex={0}
             role="region" aria-label={`${data.ticker}: the last ${recent.length} trading days`}>
          <table className="tabular w-full text-left text-xs">
            <caption className="sr-only">{data.ticker}: the last {recent.length} trading days, newest first</caption>
            <thead className="sticky top-0 bg-surface-2 text-fg-subtle">
              <tr>
                <th scope="col" className="px-2 py-1 font-medium">Day</th>
                <th scope="col" className="px-2 py-1 font-medium">Open</th>
                <th scope="col" className="px-2 py-1 font-medium">High</th>
                <th scope="col" className="px-2 py-1 font-medium">Low</th>
                <th scope="col" className="px-2 py-1 font-medium">Close</th>
                <th scope="col" className="px-2 py-1 font-medium">Volume</th>
                {keys.map((k) => (
                  <th key={k} scope="col" className="px-2 py-1 font-medium">{label(k)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {recent.map((b) => (
                <tr key={b.time} className="border-t border-border">
                  <th scope="row" className="px-2 py-1 font-normal text-fg">{b.time}</th>
                  <td className="px-2 py-1">{fmt(b.open)}</td>
                  <td className="px-2 py-1">{fmt(b.high)}</td>
                  <td className="px-2 py-1">{fmt(b.low)}</td>
                  <td className="px-2 py-1 text-fg">{fmt(b.close)}</td>
                  <td className="px-2 py-1">{b.volume.toLocaleString("en-US")}</td>
                  {keys.map((k) => (
                    <td key={k} className="px-2 py-1">{fmt(byDay[k].get(b.time))}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}

export function PriceChart({
  spec,
  token,
  height = 420,
  markers,
  onLoaded,
  visibleMonths,
}: {
  spec: ChartSpec;
  token: string;
  height?: number;
  markers?: SignalMarker[];
  onLoaded?: () => void;
  /** Open on the last N months of the data (the rest is a scroll away). */
  visibleMonths?: number;
}) {
  const container = useRef<HTMLDivElement>(null);
  const indicatorKey = spec.indicators.join(",");
  const requestKey = [spec.ticker, spec.start, spec.end, indicatorKey].join("|");
  // The result is tagged with the request it answers, so a new request shows
  // "Loading" without resetting state inside the effect.
  const [result, setResult] = useState<{
    key: string;
    data?: ChartData;
    error?: string;
  } | null>(null);
  const current = result?.key === requestKey ? result : null;
  const data = current?.data ?? null;
  const error = current?.error ?? null;

  useEffect(() => {
    let cancelled = false;
    api
      .chart(token, spec.ticker, spec.start, spec.end, indicatorKey)
      .then((d) => !cancelled && setResult({ key: requestKey, data: d }))
      .catch((e) => !cancelled && setResult({ key: requestKey, error: errorMessage(e) }));
    return () => {
      cancelled = true;
    };
  }, [token, spec.ticker, spec.start, spec.end, indicatorKey, requestKey]);

  const markerKey = JSON.stringify(markers ?? []);
  useEffect(() => {
    if (!data || !container.current) return;
    const chart = draw(container.current, data, height, JSON.parse(markerKey), visibleMonths);
    onLoaded?.();
    return () => chart.remove();
  }, [data, height, markerKey, onLoaded, visibleMonths]);

  const labels = data ? Object.values(data.indicators).map((i) => i.label) : [];
  return (
    <figure className="my-3 rounded-[var(--radius-card)] border border-border bg-surface p-3">
      <figcaption className="mb-2 flex flex-wrap items-baseline gap-x-3 text-xs text-fg-muted">
        <span className="font-medium text-fg">{spec.ticker}</span>
        <span>{spec.start} to {spec.end}</span>
        {labels.length > 0 && <span>{labels.join(" · ")}</span>}
      </figcaption>
      {error && <p className="text-xs text-down">{error}</p>}
      {!data && !error && <div aria-busy="true" className="animate-pulse rounded-md bg-surface-2" style={{ height }} />}
      {/* The canvas says nothing to a screen reader: it gets a summary, and the
          numbers it draws are in a table one click (or Enter) away. Not
          aria-hidden: the chart holds TradingView's attribution link. */}
      <div ref={container} style={{ height }} className={data ? "" : "hidden"} role="group"
           aria-label={`${spec.ticker} price chart; the figures are in the table below it`} />
      {data ? <ChartText data={data} /> : null}
      <p className="mt-2 text-[10px] text-fg-subtle">
        Prices: Yahoo Finance via yfinance, split-adjusted; returns use the dividend-adjusted
        close. Charts: TradingView Lightweight Charts™, Copyright (c) 2025 TradingView, Inc.,{" "}
        <a href="https://www.tradingview.com/" className="underline" target="_blank" rel="noreferrer">
          tradingview.com
        </a>
        .
      </p>
    </figure>
  );
}
