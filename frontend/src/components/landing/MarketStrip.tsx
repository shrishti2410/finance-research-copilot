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

import { Pause, Play } from "lucide-react";
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

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function day(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  return `${d} ${MONTHS[m - 1]} ${y}`;
}

/** The close each market's figures are from. NSE and US sessions end hours
 * apart, so the two often differ; then each is named. */
function closeLabel(entries: Entry[]): string {
  const latest: Record<string, string> = {};
  for (const e of entries) {
    const market = e.exchange === "NSE" ? "NSE" : "US";
    if (e.as_of && (!latest[market] || e.as_of > latest[market])) latest[market] = e.as_of;
  }
  const dates = Object.values(latest);
  if (!dates.length) return "Daily close";
  if (new Set(dates).size === 1) return `Daily close, ${day(dates[0])}`;
  return ["NSE", "US"].filter((m) => latest[m]).map((m) => `${m} close ${day(latest[m])}`).join(" · ");
}

export function MarketStrip() {
  const [entries, setEntries] = useState<Entry[] | null>(null);
  const [paused, setPaused] = useState(false);

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
  const list = (copy: boolean) => (
    <ul
      aria-hidden={copy || undefined}
      className={`marquee-list flex shrink-0 items-baseline gap-6 pr-6 text-sm ${copy ? "marquee-copy" : ""}`}
    >
      {entries.map((e) => {
        const up = (e.change_pct ?? 0) >= 0;
        return (
          <li key={e.ticker} className="flex items-baseline gap-2 whitespace-nowrap">
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
  );
  return (
    <section aria-label="Market strip" className="fade-in border-y border-border bg-surface/60">
      <div className="mx-auto flex max-w-6xl flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:gap-4 sm:px-6">
        {/* A slow marquee holding two copies of the list (the second hidden from
            screen readers), so it loops without a jump. It pauses on hover, on
            focus and with the button; with reduced motion it is a wrapped,
            static list (globals.css). */}
        <div className="marquee min-w-0 flex-1 overflow-hidden" data-paused={paused || undefined}>
          <div className="marquee-track flex w-max">
            {list(false)}
            {list(true)}
          </div>
        </div>
        <div className="flex shrink-0 items-center justify-between gap-3">
          <p className="text-xs text-fg-subtle">{closeLabel(entries)}</p>
          <button
            type="button"
            className="marquee-toggle rounded-md p-1 text-fg-subtle hover:text-fg"
            aria-pressed={paused}
            aria-label={paused ? "Play the market strip" : "Pause the market strip"}
            onClick={() => setPaused((v) => !v)}
          >
            {paused ? <Play className="size-3.5" aria-hidden="true" /> : <Pause className="size-3.5" aria-hidden="true" />}
          </button>
        </div>
      </div>
    </section>
  );
}
