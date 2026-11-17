"use client";

/**
 * The landing page's market strip: the two indices and the eight watchlist
 * stocks with their day change, from GET /market/strip (the stored daily
 * precompute; public, read-only).
 *
 * It renders nothing until the data is there, and nothing at all if the API
 * is asleep, slow or failing: a visitor never sees an error or a spinner
 * here. The free API can take over a minute to wake, so the request gives up
 * after STRIP_TIMEOUT_MS.
 */

import { useEffect, useState } from "react";

import { API_BASE } from "@/lib/api";

const STRIP_TIMEOUT_MS = 12_000;

type Entry = {
  ticker: string;
  exchange: string | null;
  kind: "index" | "stock";
  currency_symbol: string | null;
  price: number;
  change_pct: number | null;
  as_of: string | null;
};

const INDEX_NAMES: Record<string, string> = { "^NSEI": "Nifty 50", SPY: "S&P 500 (SPY)" };

function label(e: Entry): string {
  return INDEX_NAMES[e.ticker] ?? e.ticker.replace(/\.NS$/, "");
}

function price(e: Entry): string {
  const digits = e.price >= 1000 ? 0 : 2;
  const n = e.price.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return e.kind === "index" && e.ticker === "^NSEI" ? n : `${e.currency_symbol ?? ""}${n}`;
}

export function MarketStrip() {
  const [entries, setEntries] = useState<Entry[] | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), STRIP_TIMEOUT_MS);
    fetch(`${API_BASE}/market/strip`, { signal: ctrl.signal })
      .then((r) => (r.ok ? r.json() : null))
      .then((body) => {
        const list: Entry[] | undefined = body?.entries;
        if (Array.isArray(list) && list.length) setEntries(list);
      })
      .catch(() => {
        /* asleep, slow or failing: the strip stays hidden */
      })
      .finally(() => clearTimeout(timer));
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  }, []);

  if (!entries) return null;
  const asOf = entries.map((e) => e.as_of).filter(Boolean).sort().at(-1);
  return (
    <section aria-label="Market strip" className="fade-in border-y border-border bg-surface/60">
      <div className="mx-auto flex max-w-6xl items-center gap-4 px-4 py-3 sm:px-6">
        <ul className="flex flex-1 gap-5 overflow-x-auto whitespace-nowrap text-sm [scrollbar-width:none]">
          {entries.map((e) => {
            const up = (e.change_pct ?? 0) >= 0;
            return (
              <li key={e.ticker} className="flex items-baseline gap-2">
                <span className={e.kind === "index" ? "font-medium text-fg" : "text-fg-muted"}>{label(e)}</span>
                <span className="tabular text-fg">{price(e)}</span>
                {e.change_pct === null ? null : (
                  <span className={`tabular text-xs ${up ? "text-up" : "text-down"}`}>
                    {up ? "+" : "−"}
                    {Math.abs(e.change_pct).toFixed(2)}%
                  </span>
                )}
              </li>
            );
          })}
        </ul>
        {asOf ? <p className="hidden shrink-0 text-xs text-fg-subtle md:block">Daily close, {asOf}</p> : null}
      </div>
    </section>
  );
}
