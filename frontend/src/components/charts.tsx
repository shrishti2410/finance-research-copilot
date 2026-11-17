"use client";

// Two small SVG charts drawn from the design tokens: equity curves (signal
// against buy-and-hold) and an options payoff at expiry. Each is drawn at its
// container's measured width, so text stays 11px on a phone instead of being
// scaled down with the drawing.
import { useEffect, useRef, useState } from "react";

function useWidth(fallback = 800) {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(280, Math.round(entry.contentRect.width))));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

/** Labels drawn over a line stay legible with an outline in the card colour. */
const OUTLINE = { paintOrder: "stroke", stroke: "var(--color-surface)", strokeWidth: 4 } as const;

type Series = { label: string; values: number[]; className: string; dashed?: boolean };

function scale(min: number, max: number, from: number, to: number) {
  const span = max - min || 1;
  return (v: number) => from + ((v - min) / span) * (to - from);
}

/** Round tick values (1, 2 or 5 times a power of ten) covering min..max. */
function ticks(min: number, max: number, n = 4): number[] {
  const raw = (max - min || 1) / n;
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * power).find((m) => m >= raw) ?? raw;
  const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) out.push(Number(v.toFixed(10)));
  return out;
}

/** Growth of 100 for each series, over shared dates. */
export function EquityChart({
  days,
  series,
  height = 260,
}: {
  days: string[];
  series: Series[];
  height?: number;
}) {
  const [ref, width] = useWidth();
  const pad = { l: 44, r: 12, t: 12, b: 24 };
  const norm = series.map((s) => ({ ...s, values: s.values.map((v) => (v / s.values[0]) * 100) }));
  const all = norm.flatMap((s) => s.values);
  const min = Math.min(...all);
  const max = Math.max(...all);
  const x = scale(0, days.length - 1, pad.l, width - pad.r);
  const y = scale(min, max, height - pad.b, pad.t);
  const years = days
    .map((d, i) => ({ d, i }))
    .filter(({ d, i }) => i > 0 && d.slice(0, 4) !== days[i - 1].slice(0, 4))
    .filter((_, k, all) => width >= 480 || k % 2 === 0 || k === all.length - 1);
  const summary = norm
    .map((s) => `${s.label}: 100 to ${s.values[s.values.length - 1].toFixed(0)}`)
    .join("; ");
  return (
    <figure ref={ref} className="flex flex-col gap-2">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width={width}
        height={height}
        className="max-w-full"
        role="img"
        aria-label={`Growth of 100 from ${days[0]} to ${days[days.length - 1]}. ${summary}.`}
      >
        {ticks(min, max).map((t) => (
          <g key={t}>
            <line x1={pad.l} x2={width - pad.r} y1={y(t)} y2={y(t)} className="stroke-border" strokeWidth="1" />
            <text x={pad.l - 6} y={y(t) + 4} textAnchor="end" className="fill-fg-subtle text-[11px]">
              {t.toFixed(0)}
            </text>
          </g>
        ))}
        {years.map(({ d, i }) => (
          <text key={d} x={x(i)} y={height - 6} textAnchor="middle" className="fill-fg-subtle text-[11px]">
            {d.slice(0, 4)}
          </text>
        ))}
        {norm.map((s) => (
          <polyline
            key={s.label}
            fill="none"
            strokeWidth="2"
            strokeDasharray={s.dashed ? "5 4" : undefined}
            className={s.className}
            points={s.values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ")}
          />
        ))}
      </svg>
      <figcaption className="flex flex-wrap gap-4 text-xs text-fg-muted">
        {norm.map((s) => (
          <span key={s.label} className="inline-flex items-center gap-1.5">
            <svg width="18" height="6" aria-hidden="true">
              <line x1="0" x2="18" y1="3" y2="3" strokeWidth="2" strokeDasharray={s.dashed ? "4 3" : undefined} className={s.className} />
            </svg>
            {s.label}
          </span>
        ))}
        <span className="text-fg-subtle">Growth of 100, after trading costs (Indian delivery charges on NSE, 5 bps a side in the US).</span>
      </figcaption>
    </figure>
  );
}

/** Profit at expiry against the stock price, with the zero line, spot and breakevens. */
export function PayoffChart({
  points,
  spot,
  breakevens,
  symbol,
  height = 260,
}: {
  points: { price: number; profit: number }[];
  spot: number;
  breakevens: number[];
  symbol: string;
  height?: number;
}) {
  const [ref, width] = useWidth();
  const pad = { l: 48, r: 12, t: 16, b: 28 };
  const prices = points.map((p) => p.price);
  const profits = points.map((p) => p.profit);
  const lo = Math.min(...profits, 0);
  const hi = Math.max(...profits, 0);
  const x = scale(prices[0], prices[prices.length - 1], pad.l, width - pad.r);
  const y = scale(lo, hi, height - pad.b, pad.t);
  const line = points.map((p) => `${x(p.price).toFixed(1)},${y(p.profit).toFixed(1)}`).join(" ");
  // Shade above and below zero separately: gains green, losses red.
  const area = `${x(prices[0])},${y(0)} ${line} ${x(prices[prices.length - 1])},${y(0)}`;
  return (
    <div ref={ref}>
    <svg
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      className="max-w-full"
      role="img"
      aria-label={`Profit at expiry from ${symbol}${prices[0].toFixed(0)} to ${symbol}${prices[prices.length - 1].toFixed(0)}; breakeven at ${breakevens.map((b) => symbol + b.toFixed(2)).join(" and ") || "none"}.`}
    >
      <defs>
        <clipPath id="above"><rect x="0" y="0" width={width} height={y(0)} /></clipPath>
        <clipPath id="below"><rect x="0" y={y(0)} width={width} height={height} /></clipPath>
      </defs>
      <polygon points={area} className="fill-up/15" clipPath="url(#above)" />
      <polygon points={area} className="fill-down/15" clipPath="url(#below)" />
      {ticks(lo, hi).map((t) => (
        <text key={t} x={pad.l - 6} y={y(t) + 4} textAnchor="end" className="fill-fg-subtle text-[11px]">
          {t.toFixed(0)}
        </text>
      ))}
      <line x1={pad.l} x2={width - pad.r} y1={y(0)} y2={y(0)} className="stroke-fg-subtle" strokeWidth="1" />
      <polyline points={line} fill="none" strokeWidth="2.5" className="stroke-fg" />
      <line x1={x(spot)} x2={x(spot)} y1={pad.t} y2={height - pad.b} className="stroke-accent" strokeDasharray="4 4" />
      <text x={x(spot)} y={height - 8} textAnchor="middle" className="fill-accent text-[11px]" style={OUTLINE}>
        spot {symbol}{spot.toFixed(0)}
      </text>
      {breakevens.map((b) => (
        <g key={b}>
          <circle cx={x(b)} cy={y(0)} r="4" className="fill-bg stroke-fg" strokeWidth="2" />
          <text x={x(b)} y={y(0) + (y(0) > height / 2 ? -10 : 18)} textAnchor="middle" className="fill-fg text-[11px]" style={OUTLINE}>
            {symbol}{b.toFixed(0)}
          </text>
        </g>
      ))}
    </svg>
    </div>
  );
}
