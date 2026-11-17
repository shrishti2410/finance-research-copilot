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

function points(data: ChartData, key: string): Point[] {
  return (data.series[key] ?? []).map((p) => ({ time: p.time as Time, value: p.value }));
}

function draw(container: HTMLElement, data: ChartData, height: number): IChartApi {
  const chart = createChart(container, {
    height,
    autoSize: true,
    layout: {
      // TradingView's licence: the logo links to tradingview.com. Left on.
      attributionLogo: true,
      background: { type: ColorType.Solid, color: "#0a0a0a" },
      textColor: "#a3a3a3",
      panes: { separatorColor: "#262626" },
    },
    grid: { vertLines: { color: "#171717" }, horzLines: { color: "#171717" } },
    timeScale: { borderColor: "#262626" },
    rightPriceScale: { borderColor: "#262626" },
  });

  const candles = chart.addSeries(CandlestickSeries, {
    upColor: "#22c55e", downColor: "#ef4444", borderVisible: false,
    wickUpColor: "#22c55e", wickDownColor: "#ef4444",
  });
  candles.setData(data.bars.map((b) => ({
    time: b.time as Time, open: b.open, high: b.high, low: b.low, close: b.close,
  })));

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
            ...p, color: up.get(p.time as string) ? "#14532d" : "#7f1d1d",
          })),
        );
      } else if (key === "macd_hist") {
        chart.addSeries(HistogramSeries, {}, pane).setData(
          points(data, key).map((p) => ({ ...p, color: p.value >= 0 ? "#166534" : "#991b1b" })),
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
  chart.timeScale().fitContent();
  return chart;
}

export function chartHref(spec: ChartSpec): string {
  const query = new URLSearchParams({
    ticker: spec.ticker, start: spec.start, end: spec.end,
    indicators: spec.indicators.join(","),
  });
  return `/chart?${query.toString()}`;
}

export function PriceChart({
  spec,
  token,
  height = 420,
}: {
  spec: ChartSpec;
  token: string;
  height?: number;
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

  useEffect(() => {
    if (!data || !container.current) return;
    const chart = draw(container.current, data, height);
    return () => chart.remove();
  }, [data, height]);

  const labels = data ? Object.values(data.indicators).map((i) => i.label) : [];
  return (
    <figure className="my-3 rounded-md border border-neutral-800 bg-neutral-950 p-2">
      <figcaption className="mb-2 flex flex-wrap items-baseline gap-x-3 text-xs text-neutral-400">
        <span className="font-medium text-neutral-200">{spec.ticker}</span>
        <span>{spec.start} to {spec.end}</span>
        {labels.length > 0 && <span>{labels.join(" · ")}</span>}
      </figcaption>
      {error && <p className="text-xs text-red-300">{error}</p>}
      {!data && !error && <p className="text-xs text-neutral-500">Loading prices…</p>}
      <div ref={container} style={{ height }} className={data ? "" : "hidden"} />
      <p className="mt-2 text-[10px] text-neutral-600">
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
