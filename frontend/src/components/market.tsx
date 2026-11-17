// Small market pieces shared by the dashboard and the stock page.
import { ArrowDownRight, ArrowUpRight, Minus } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn, formatPct } from "@/lib/utils";

/** A day's change. Green and red only here and on BUY/SELL. */
export function Change({ pct, className }: { pct: number | null | undefined; className?: string }) {
  const dir = pct === null || pct === undefined || pct === 0 ? 0 : pct > 0 ? 1 : -1;
  const Icon = dir > 0 ? ArrowUpRight : dir < 0 ? ArrowDownRight : Minus;
  return (
    <span
      className={cn(
        "tabular inline-flex items-center gap-0.5 text-sm font-medium",
        dir > 0 ? "text-up" : dir < 0 ? "text-down" : "text-fg-muted",
        className,
      )}
    >
      <Icon className="size-3.5" aria-hidden="true" />
      {formatPct(pct)}
    </span>
  );
}

export type SignalValue = "BUY" | "SELL" | "HOLD";

/** The engine's output with its coverage: "BUY · 12 of 16 rules". */
export function SignalBadge({
  signal,
  coverage,
  className,
}: {
  signal: SignalValue;
  coverage?: string | null;
  className?: string;
}) {
  const variant = signal === "BUY" ? "buy" : signal === "SELL" ? "sell" : "neutral";
  return (
    <span className={cn("inline-flex flex-wrap items-center gap-2", className)}>
      <Badge variant={variant} aria-label={`Signal engine output: ${signal}`}>
        {signal}
      </Badge>
      {coverage ? <span className="text-xs text-fg-subtle">on {coverage}</span> : null}
    </span>
  );
}

/** A tiny line of closing prices; colour by the change over the window. */
export function Sparkline({
  values,
  width = 120,
  height = 36,
  className,
}: {
  values: number[];
  width?: number;
  height?: number;
  className?: string;
}) {
  if (values.length < 2) return <div style={{ width, height }} className={className} />;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const step = width / (values.length - 1);
  const points = values
    .map((v, i) => `${(i * step).toFixed(1)},${(height - 2 - ((v - min) / span) * (height - 4)).toFixed(1)}`)
    .join(" ");
  const up = values[values.length - 1] >= values[0];
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      className={cn(up ? "text-up" : "text-down", className)}
      role="img"
      aria-label={`${values.length}-day price line, ${up ? "up" : "down"} over the period`}
    >
      <polyline points={points} fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinejoin="round" />
    </svg>
  );
}

export const COMPANY_NAMES: Record<string, string> = {
  "^NSEI": "Nifty 50",
  "COFORGE.NS": "Coforge",
  "DIXON.NS": "Dixon Technologies",
  "SBIN.NS": "State Bank of India",
  "PREMIERENE.NS": "Premier Energies",
  "HDFCBANK.NS": "HDFC Bank",
};

/** "SBIN.NS" -> "SBIN" for display; the full symbol stays in URLs. */
export function shortTicker(ticker: string): string {
  return ticker.replace(/\.(NS|BO)$/, "").replace(/^\^NSEI$/, "NIFTY 50");
}
