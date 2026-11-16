"use client";

import { ChevronRight } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { COMPANY_NAMES, Change, SignalBadge, Sparkline, shortTicker } from "@/components/market";
import { ColdStartNotice, EmptyState, ErrorState, useSlow } from "@/components/states";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api, type Watchlist, type WatchlistCard } from "@/lib/api";
import { errorMessage, useAuth } from "@/lib/auth";
import { formatPrice } from "@/lib/utils";

function IndexStrip({ data }: { data: Watchlist["index"] }) {
  if (!data) return null;
  return (
    <Card className="flex flex-wrap items-center gap-x-6 gap-y-2 px-5 py-4">
      <div>
        <p className="text-xs font-medium uppercase tracking-wider text-fg-subtle">Nifty 50</p>
        <p className="tabular text-2xl font-semibold tracking-tight">{formatPrice(data.price, "", true)}</p>
      </div>
      <Change pct={data.change_pct} className="text-base" />
      <Sparkline values={data.sparkline} width={220} height={44} className="ml-auto hidden sm:block" />
      <p className="w-full text-xs text-fg-subtle sm:w-auto">Close of {data.as_of}</p>
    </Card>
  );
}

function StockCard({ card }: { card: WatchlistCard }) {
  const name = COMPANY_NAMES[card.ticker] ?? card.ticker;
  if (card.error) {
    return (
      <Card className="p-5">
        <p className="font-medium">{name}</p>
        <p className="mt-2 text-sm text-fg-muted">No price history right now.</p>
      </Card>
    );
  }
  const sig = card.signal && "signal" in card.signal ? card.signal : null;
  return (
    <Link
      href={`/stock/${encodeURIComponent(card.ticker)}`}
      className="group rounded-[var(--radius-card)] focus-visible:outline-2"
      aria-label={`${name}: open the stock page`}
    >
      <Card className="flex h-full flex-col gap-4 p-5 transition-colors group-hover:border-fg-subtle">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="truncate font-medium text-fg">{name}</p>
            <p className="text-xs text-fg-subtle">NSE: {shortTicker(card.ticker)}</p>
          </div>
          <ChevronRight className="size-4 text-fg-subtle transition-transform group-hover:translate-x-0.5" aria-hidden="true" />
        </div>
        <div className="flex items-end justify-between gap-3">
          <div>
            <p className="tabular text-xl font-semibold tracking-tight">{formatPrice(card.price, card.currency_symbol)}</p>
            <Change pct={card.change_pct} />
          </div>
          <div className="flex flex-col items-end gap-0.5">
            <Sparkline values={card.sparkline} />
            <span className="text-[10px] text-fg-subtle">60 days</span>
          </div>
        </div>
        <div className="mt-auto flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3">
          {sig ? (
            <SignalBadge signal={sig.signal} coverage={sig.coverage} />
          ) : (
            <span className="text-xs text-fg-subtle">No signal yet</span>
          )}
          {sig?.warnings.length ? (
            <span className="text-[11px] text-warn">{sig.warnings.join(" · ")}</span>
          ) : null}
        </div>
      </Card>
    </Link>
  );
}

function Loading() {
  return (
    <div aria-busy="true" aria-label="Loading the watchlist" className="flex flex-col gap-4">
      <Skeleton className="h-20 w-full rounded-[var(--radius-card)]" />
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {Array.from({ length: 5 }).map((_, i) => (
          <Skeleton key={i} className="h-48 rounded-[var(--radius-card)]" />
        ))}
      </div>
    </div>
  );
}

export default function Dashboard() {
  const { token, ready } = useAuth();
  const router = useRouter();
  const [data, setData] = useState<Watchlist | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const slow = useSlow(loading);

  useEffect(() => {
    if (ready && !token) router.replace("/login");
  }, [ready, token, router]);

  // State is set only when the request settles, so the effect never sets
  // state synchronously; `live` drops a response that arrives after unmount.
  const fetchInto = useCallback(
    (live: () => boolean) => {
      if (!token) return;
      api
        .watchlist(token)
        .then((d) => live() && (setData(d), setError(null)))
        .catch((err) => live() && setError(errorMessage(err)))
        .finally(() => live() && setLoading(false));
    },
    [token],
  );

  useEffect(() => {
    let alive = true;
    fetchInto(() => alive);
    return () => {
      alive = false;
    };
  }, [fetchInto]);

  const retry = () => {
    setLoading(true);
    setError(null);
    fetchInto(() => true);
  };

  return (
    <main className="mx-auto flex max-w-6xl flex-col gap-6 px-4 py-8 sm:px-6">
      <div className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">Watchlist</h1>
        <p className="text-sm text-fg-muted">
          Five NSE stocks against the Nifty 50. Each badge is the signal engine&apos;s output and how many of
          its rules carry weight; the backtest behind it is on each stock&apos;s page.
        </p>
      </div>
      {slow && loading ? <ColdStartNotice /> : null}
      {loading && !data ? <Loading /> : null}
      {error ? <ErrorState message={error} onRetry={retry} /> : null}
      {data ? (
        <>
          <IndexStrip data={data.index} />
          {data.stocks.length ? (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {data.stocks.map((c) => (
                <StockCard key={c.ticker} card={c} />
              ))}
            </div>
          ) : (
            <EmptyState title="No stocks in the watchlist" />
          )}
          <p className="text-xs text-fg-subtle">
            Prices are daily closes from {data.provider === "yfinance" ? "Yahoo Finance" : data.provider}.
            Signals are a vote of textbook rules computed from past prices, not forecasts.
          </p>
        </>
      ) : null}
    </main>
  );
}
