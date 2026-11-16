"use client";

/**
 * /chart?ticker=NVDA&start=2025-11-01&end=2026-11-13&indicators=sma50,rsi14
 *
 * The chart page: pick a ticker, a date range and indicators. The URL holds
 * the choice, so a chart the agent drew in the chat opens here as it was.
 * useSearchParams sits inside a Suspense boundary, as the Next.js docs for
 * this version require of a client component that reads the URL.
 */

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { PriceChart } from "@/components/PriceChart";
import type { ChartSpec } from "@/lib/api";
import { useAuth } from "@/lib/auth";

const INDICATORS: { key: string; label: string }[] = [
  { key: "sma20", label: "SMA 20" },
  { key: "sma50", label: "SMA 50" },
  { key: "sma200", label: "SMA 200" },
  { key: "ema12", label: "EMA 12" },
  { key: "ema26", label: "EMA 26" },
  { key: "bollinger", label: "Bollinger" },
  { key: "volume", label: "Volume" },
  { key: "rsi14", label: "RSI 14" },
  { key: "macd", label: "MACD" },
  { key: "atr14", label: "ATR 14" },
  { key: "obv", label: "OBV" },
];

function isoDaysAgo(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

function ChartWorkspace() {
  const params = useSearchParams();
  const router = useRouter();
  const { token, ready } = useAuth();

  const spec: ChartSpec = {
    kind: "price_chart",
    ticker: (params.get("ticker") || "NVDA").toUpperCase(),
    start: params.get("start") || isoDaysAgo(365),
    end: params.get("end") || isoDaysAgo(0),
    indicators: (params.get("indicators") ?? "volume,sma50")
      .split(",").map((s) => s.trim()).filter(Boolean),
  };
  const [draft, setDraft] = useState(spec);

  useEffect(() => {
    if (ready && !token) router.replace("/login");
  }, [ready, token, router]);

  function show(next: ChartSpec) {
    const query = new URLSearchParams({
      ticker: next.ticker.toUpperCase(), start: next.start, end: next.end,
      indicators: next.indicators.join(","),
    });
    router.replace(`/chart?${query.toString()}`);
  }

  if (!ready || !token) {
    return <p className="p-6 text-sm text-fg-subtle">Loading…</p>;
  }

  return (
    <main className="mx-auto max-w-5xl px-4 py-6">
      <div className="mb-4 flex items-baseline justify-between">
        <h1 className="text-lg font-medium">Price chart</h1>
        <Link href="/research" className="text-xs text-fg-muted underline">Back to the chat</Link>
      </div>

      <form
        className="mb-4 flex flex-wrap items-end gap-3 text-sm"
        onSubmit={(e) => {
          e.preventDefault();
          show(draft);
        }}
      >
        <label className="flex flex-col text-xs text-fg-muted">
          Ticker
          <input
            value={draft.ticker}
            onChange={(e) => setDraft({ ...draft, ticker: e.target.value })}
            className="mt-1 w-24 rounded-md border border-border bg-surface px-2 py-1 text-sm text-fg uppercase"
          />
        </label>
        <label className="flex flex-col text-xs text-fg-muted">
          From
          <input
            type="date"
            value={draft.start}
            onChange={(e) => setDraft({ ...draft, start: e.target.value })}
            className="mt-1 rounded-md border border-border bg-surface px-2 py-1 text-sm text-fg"
          />
        </label>
        <label className="flex flex-col text-xs text-fg-muted">
          To
          <input
            type="date"
            value={draft.end}
            onChange={(e) => setDraft({ ...draft, end: e.target.value })}
            className="mt-1 rounded-md border border-border bg-surface px-2 py-1 text-sm text-fg"
          />
        </label>
        <button
          type="submit"
          className="rounded-md bg-accent-strong px-3 py-1.5 text-sm font-medium text-bg"
        >
          Show
        </button>
        <fieldset className="flex w-full flex-wrap gap-x-4 gap-y-1 text-xs text-fg-muted">
          {INDICATORS.map((ind) => (
            <label key={ind.key} className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={draft.indicators.includes(ind.key)}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    indicators: e.target.checked
                      ? [...draft.indicators, ind.key]
                      : draft.indicators.filter((k) => k !== ind.key),
                  })
                }
              />
              {ind.label}
            </label>
          ))}
        </fieldset>
      </form>

      <PriceChart spec={spec} token={token} height={560} />
    </main>
  );
}

export default function ChartPage() {
  return (
    <Suspense fallback={<p className="p-6 text-sm text-fg-subtle">Loading…</p>}>
      <ChartWorkspace />
    </Suspense>
  );
}
