"use client";

import { ArrowLeft, BookOpen, MessageSquare, Sparkles } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";

import { EquityChart, PayoffChart } from "@/components/charts";
import { Markdown } from "@/components/Markdown";
import { COMPANY_NAMES, SignalBadge, shortTicker } from "@/components/market";
import { PriceChart } from "@/components/PriceChart";
import { GITHUB_URL } from "@/components/SiteChrome";
import { ColdStartNotice, EmptyState, ErrorState, useSlow } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  api,
  type OptionIdeas,
  type RiskFigures,
  type SignalExplanation,
  type SignalReport,
} from "@/lib/api";
import { errorMessage, useAuth } from "@/lib/auth";
import { cn, formatPct } from "@/lib/utils";

// Strategy cards are linked on GitHub at the branch that has them.
const DOCS_REF = "feat/v2-market";
// The agent's explanation runs on local Ollama only, so a deployed build hides
// it until it is wired to the hosted model. Set NEXT_PUBLIC_ENABLE_AGENT_EXPLAIN=1
// at build time to show it (read at build, like NEXT_PUBLIC_API_BASE).
const AGENT_EXPLAIN = process.env.NEXT_PUBLIC_ENABLE_AGENT_EXPLAIN === "1";
const TABS = ["chart", "signal", "backtest", "options", "research"] as const;
type Tab = (typeof TABS)[number];

function isoDaysAgo(days: number) {
  return new Date(Date.now() - days * 86_400_000).toISOString().slice(0, 10);
}

function pct(v: number | null | undefined, digits = 1) {
  return v === null || v === undefined ? "–" : formatPct(v * 100, digits);
}

function num(v: number | null | undefined, digits = 2) {
  return v === null || v === undefined ? "–" : v.toFixed(digits);
}

function Verdict({ verdict }: { verdict: SignalReport["verdict"] }) {
  const noEdge = verdict.verdict !== "edge";
  return (
    <div
      className={cn(
        "rounded-[var(--radius-card)] border p-4 text-sm",
        noEdge ? "border-warn/40 bg-surface-2" : "border-accent/40 bg-surface-2",
      )}
    >
      <div className="mb-1 flex items-center gap-2">
        <Badge variant={noEdge ? "warn" : "accent"}>
          {verdict.verdict === "no_edge" ? "No edge" : verdict.verdict === "edge" ? "Edge" : "Untested"}
        </Badge>
        <span className="text-xs text-fg-subtle">Five-year backtest, out of sample</span>
      </div>
      <p className="text-fg">{verdict.statement}</p>
    </div>
  );
}

function TabSkeleton({ label }: { label: string }) {
  return (
    <div aria-busy="true" aria-label={label} className="flex flex-col gap-4">
      <Skeleton className="h-20 w-full rounded-[var(--radius-card)]" />
      <Skeleton className="h-64 w-full rounded-[var(--radius-card)]" />
    </div>
  );
}

/**
 * The regime label's direction comes from the 50-day average's 20-day slope,
 * which lags a sharp turn. When the DI lines (part of the same ADX reading)
 * point the other way, the page says so rather than show a label its own
 * numbers contradict. The engine's rule is unchanged: a rule that required
 * both to agree was tested on ten hold-out stocks and not adopted
 * (benchmarks/REGIME_RULE_CHECK.md).
 */
function diDisagreement(regime: SignalReport["evidence"]["regime"]): string | null {
  const { label, plus_di: plus, minus_di: minus } = regime;
  if (plus === null || minus === null) return null;
  if (label === "trending_down" && plus > minus) return "up";
  if (label === "trending_up" && minus > plus) return "down";
  return null;
}

function SignalTab({ report, token, ticker }: { report: SignalReport; token: string; ticker: string }) {
  const ev = report.evidence;
  const [explain, setExplain] = useState<{ busy: boolean; data?: SignalExplanation; error?: string }>({ busy: false });
  const slow = useSlow(explain.busy, 8000);
  if (!ev.ok) return <EmptyState title="No signal yet">{ev.error}</EmptyState>;
  const cards = new Map(report.cards.map((c) => [c.strategy, c]));
  const counted = ev.votes.filter((v) => v.weight > 0);
  const disagree = diDisagreement(ev.regime);
  return (
    <div className="flex flex-col gap-6" data-loaded="signal">
      <Verdict verdict={report.verdict} />
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <Card className="p-5">
          <p className="text-xs uppercase tracking-wider text-fg-subtle">Engine output, {ev.as_of}</p>
          <div className="mt-3">
            <SignalBadge signal={ev.signal} />
          </div>
          <p className="tabular mt-3 text-sm text-fg-muted">
            Score {ev.score >= 0 ? "+" : ""}{ev.score.toFixed(3)} (BUY at +{ev.thresholds.buy_at}, SELL at {ev.thresholds.sell_at})
          </p>
        </Card>
        <Card className="p-5">
          <p className="text-xs uppercase tracking-wider text-fg-subtle">Coverage and confidence</p>
          <p className="mt-3 text-lg font-semibold">{ev.coverage.text}</p>
          <div className="mt-2 h-2 overflow-hidden rounded-full bg-surface-2" aria-hidden="true">
            <div className="h-full bg-accent" style={{ width: `${ev.coverage.fraction * 100}%` }} />
          </div>
          <p className="tabular mt-3 text-sm text-fg-muted">
            Confidence {ev.confidence.toFixed(2)}
            {ev.confidence < ev.confidence_uncapped ? ` (capped from ${ev.confidence_uncapped.toFixed(2)} by coverage)` : ""}
          </p>
        </Card>
        <Card className="p-5">
          <p className="text-xs uppercase tracking-wider text-fg-subtle">Market regime</p>
          <p className="mt-3 text-lg font-semibold capitalize">{ev.regime.label.replace("_", " ")}</p>
          <p className="tabular mt-2 text-sm text-fg-muted">
            ADX {num(ev.regime.adx, 1)} · +DI {num(ev.regime.plus_di, 1)} / −DI {num(ev.regime.minus_di, 1)} · volatility
            percentile {pct(ev.regime.volatility_percentile, 0).replace("+", "")}
          </p>
          {disagree ? (
            <p role="note" className="mt-3 rounded-md border border-warn/40 bg-surface-2 px-3 py-2 text-xs text-warn">
              The DI lines point {disagree} (+DI {num(ev.regime.plus_di, 1)} vs −DI {num(ev.regime.minus_di, 1)}), against this
              label. The label&apos;s direction comes from the 50-day average&apos;s 20-day slope, which lags a sharp
              turn: after a fall and a rebound, the average is still falling. A rule requiring both to agree was tested on
              ten unseen stocks and not adopted.
            </p>
          ) : null}
        </Card>
      </div>
      {ev.warnings.length ? (
        <ul className="flex flex-col gap-2 text-sm">
          {ev.warnings.map((w) => (
            <li key={w} className="rounded-md border border-warn/30 bg-surface px-3 py-2 text-warn">{w}</li>
          ))}
        </ul>
      ) : null}
      <section aria-labelledby="reasons">
        <h2 id="reasons" className="mb-3 font-semibold">Why: the rules that carry weight</h2>
        <ol className="flex flex-col gap-3">
          {counted.slice(0, 6).map((v) => {
            const card = cards.get(v.strategy);
            return (
              <li key={v.strategy}>
                <Card className="p-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="font-medium">{v.name}</p>
                    <span className="tabular text-xs text-fg-muted">
                      {(v.share_of_vote * 100).toFixed(0)}% of the vote · training Sharpe {v.train_sharpe.toFixed(2)}
                    </span>
                  </div>
                  <p className="mt-1 text-sm">
                    <span className={v.position === "in" ? "text-up" : "text-down"}>
                      {v.position === "in" ? "Votes BUY · in the stock" : "Votes SELL · out of the stock"}
                    </span>
                    <span className="text-fg-subtle"> · {v.style} rule</span>
                  </p>
                  {card?.summary ? <p className="mt-2 text-sm text-fg-muted">{card.summary}</p> : null}
                  {card?.when_it_fails ? (
                    <p className="mt-2 text-sm text-fg-muted">
                      <span className="font-medium text-fg">When it fails: </span>
                      {card.when_it_fails}
                    </p>
                  ) : null}
                  <a
                    href={`${GITHUB_URL}/blob/${DOCS_REF}/${v.card}`}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="mt-2 inline-flex items-center gap-1 text-xs text-accent underline"
                  >
                    <BookOpen className="size-3.5" aria-hidden="true" />
                    {v.card}
                  </a>
                </Card>
              </li>
            );
          })}
        </ol>
        {ev.votes.length > counted.length ? (
          <p className="mt-3 text-xs text-fg-subtle">
            {ev.votes.length - counted.length} more rules had no positive Sharpe ratio in training and count for nothing.
          </p>
        ) : null}
      </section>
      {AGENT_EXPLAIN ? (
      <section aria-labelledby="agent" className="flex flex-col gap-3">
        <h2 id="agent" className="font-semibold">In words</h2>
        {!explain.data ? (
          <div className="flex flex-col items-start gap-2">
            <Button
              variant="secondary"
              disabled={explain.busy}
              onClick={async () => {
                setExplain({ busy: true });
                try {
                  setExplain({ busy: false, data: await api.explainSignal(token, ticker) });
                } catch (err) {
                  setExplain({ busy: false, error: errorMessage(err) });
                }
              }}
            >
              <Sparkles className="size-4" aria-hidden="true" />
              {explain.busy ? "Writing the explanation…" : "Ask the agent to explain"}
            </Button>
            <p className="text-xs text-fg-subtle">
              A language model explains the evidence above, citing the cards. It can take a few minutes.
            </p>
            {slow ? <ColdStartNotice /> : null}
            {explain.error ? <ErrorState message={explain.error} /> : null}
          </div>
        ) : (
          <Card className="p-5">
            <Markdown text={explain.data.answer} />
          </Card>
        )}
      </section>
      ) : null}
    </div>
  );
}

function RiskTable({ risk }: { risk: SignalReport["evaluation"]["risk"] }) {
  const rows: [string, RiskFigures | null][] = [
    ["Signal", risk.signal],
    ["Buy and hold", risk.buy_and_hold],
    ["Nifty 50", risk.nifty],
  ];
  return (
    <div className="overflow-x-auto">
      <table className="tabular w-full min-w-[560px] text-left text-sm">
        <caption className="sr-only">Risk-adjusted figures over the backtest window</caption>
        <thead className="text-xs text-fg-subtle">
          <tr>
            {["", "CAGR", "Max drawdown", "Sharpe", "Sortino", "Calmar", "Time in market"].map((h) => (
              <th key={h} scope="col" className="border-b border-border py-2 pr-4 font-medium">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, r]) =>
            r ? (
              <tr key={label}>
                <th scope="row" className="border-b border-border/60 py-2 pr-4 font-medium">{label}</th>
                <td className="border-b border-border/60 py-2 pr-4">{pct(r.cagr)}</td>
                <td className="border-b border-border/60 py-2 pr-4">{pct(r.max_drawdown)}</td>
                <td className="border-b border-border/60 py-2 pr-4">{num(r.sharpe)}</td>
                <td className="border-b border-border/60 py-2 pr-4">{num(r.sortino)}</td>
                <td className="border-b border-border/60 py-2 pr-4">{num(r.calmar)}</td>
                <td className="border-b border-border/60 py-2 pr-4">{pct(r.time_in_market, 0).replace("+", "")}</td>
              </tr>
            ) : null,
          )}
        </tbody>
      </table>
    </div>
  );
}

function BacktestTab({ report }: { report: SignalReport }) {
  const e = report.evaluation;
  if (!e.ok || !e.curves) return <EmptyState title="Not enough history to backtest the signal" />;
  return (
    <div className="flex flex-col gap-6" data-loaded="backtest">
      <Verdict verdict={report.verdict} />
      <Card className="p-5">
        <h2 className="mb-3 font-semibold">The signal against simply holding, {e.start} to {e.end}</h2>
        <EquityChart
          days={e.curves.days}
          series={[
            { label: "Signal (in or out)", values: e.curves.signal, className: "stroke-accent" },
            { label: "Buy and hold", values: e.curves.buy_and_hold, className: "stroke-fg-muted", dashed: true },
          ]}
        />
      </Card>
      <Card className="p-5">
        <h2 className="mb-3 font-semibold">Risk-adjusted</h2>
        <RiskTable risk={e.risk} />
        <p className="mt-3 text-xs text-fg-subtle">
          Daily Sharpe and Sortino, no risk-free rate; Calmar is CAGR over the largest drawdown. The signal&apos;s Sharpe beat{" "}
          {e.random.signal_sharpe_percentile === null ? "–" : `${Math.round(e.random.signal_sharpe_percentile * 100)}%`} of{" "}
          {e.random.runs} random re-timings of its own trades; 95% would be needed to rule out luck.
        </p>
      </Card>
      <p className="text-sm text-fg-muted">
        Want to test a single strategy instead?{" "}
        <Link href="/backtest" className="text-accent underline">Open the strategy backtester</Link>.
      </p>
    </div>
  );
}

function OptionsTab({ token, ticker }: { token: string; ticker: string }) {
  const [structure, setStructure] = useState<string | null>(null);
  const [state, setState] = useState<{ key: string; data?: OptionIdeas; error?: string } | null>(null);
  const key = structure ?? "suggested";
  const current = state?.key === key ? state : null;
  const slow = useSlow(!current, 4000);
  const [picked, setPicked] = useState(0);

  useEffect(() => {
    let live = true;
    api
      .optionIdeas(token, ticker, structure ?? undefined)
      .then((d) => live && setState({ key, data: d }))
      .catch((err) => live && setState({ key, error: errorMessage(err) }));
    return () => {
      live = false;
    };
  }, [token, ticker, structure, key]);

  if (current?.error) return <ErrorState message={current.error} />;
  if (!current?.data)
    return (
      <>
        {slow ? <ColdStartNotice /> : null}
        <TabSkeleton label="Loading option structures" />
      </>
    );
  const d = current.data;
  const s = d.structures[Math.min(picked, d.structures.length - 1)];
  const sym = d.currency_symbol;
  const money = (v: number | null, unbounded: boolean) =>
    unbounded ? "Unlimited" : v === null ? "–" : `${v < 0 ? "−" : ""}${sym}${Math.abs(v).toFixed(2)}`;
  const ALL = ["long_call", "long_put", "covered_call", "protective_put", "bull_call_spread", "bear_put_spread", "long_straddle", "iron_condor"];
  return (
    <div className="flex flex-col gap-6" data-loaded="options">
      <div className="rounded-[var(--radius-card)] border border-warn/40 bg-surface-2 p-4 text-sm">
        <p className="text-fg">
          Premiums are <strong>Black-Scholes estimates</strong> from {shortTicker(d.ticker)}&apos;s 30-day historical
          volatility ({(d.volatility_30d * 100).toFixed(1)}%), not market quotes. These structures follow the engine&apos;s{" "}
          {d.signal.signal} on {d.signal.coverage}, and that signal&apos;s backtest verdict is{" "}
          <strong>{d.backtest.verdict === "no_edge" ? "no edge" : d.backtest.verdict}</strong>.
        </p>
      </div>
      <div className="flex flex-wrap gap-2" role="group" aria-label="Choose a structure">
        {(structure ? [structure] : d.structures.map((x) => x.structure)).concat(
          ALL.filter((k) => !d.structures.some((x) => x.structure === k) && k !== structure),
        ).map((k, i) => {
          const suggested = !structure && i < d.structures.length;
          const active = suggested ? i === picked : k === structure;
          return (
            <button
              key={k}
              onClick={() => {
                if (suggested) setPicked(i);
                else {
                  setStructure(k);
                  setPicked(0);
                }
              }}
              aria-pressed={active}
              className={cn(
                "rounded-full border px-3 py-1 text-xs",
                active ? "border-accent bg-surface-2 text-fg" : "border-border text-fg-muted hover:text-fg",
              )}
            >
              {k.replaceAll("_", " ")}
              {suggested ? " · matches the signal" : ""}
            </button>
          );
        })}
        {structure ? (
          <button onClick={() => setStructure(null)} className="px-2 text-xs text-accent underline">
            back to the suggested ones
          </button>
        ) : null}
      </div>
      <Card className="p-5">
        <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="font-semibold">{s.name}</h2>
          <span className="text-xs text-fg-subtle">View: {s.view} · {s.days}-day expiry · per share</span>
        </div>
        <PayoffChart points={s.payoff} spot={s.spot} breakevens={s.breakevens} symbol={sym} />
        <dl className="tabular mt-4 grid grid-cols-2 gap-4 text-sm sm:grid-cols-4">
          <div><dt className="text-xs text-fg-subtle">Max loss</dt><dd className="font-semibold text-down">{money(s.max_loss, s.max_loss_unbounded)}</dd></div>
          <div><dt className="text-xs text-fg-subtle">Max profit</dt><dd className="font-semibold text-up">{money(s.max_profit, s.max_profit_unbounded)}</dd></div>
          <div><dt className="text-xs text-fg-subtle">Breakeven</dt><dd className="font-semibold">{s.breakevens.map((b) => `${sym}${b.toFixed(2)}`).join(" / ") || "–"}</dd></div>
          <div><dt className="text-xs text-fg-subtle">Net premium (est.)</dt><dd className="font-semibold">{s.net_premium_estimate >= 0 ? "receive " : "pay "}{sym}{Math.abs(s.net_premium_estimate).toFixed(2)}</dd></div>
        </dl>
        <div className="mt-4 border-t border-border pt-4">
          <p className="mb-2 text-xs uppercase tracking-wider text-fg-subtle">Greeks (estimates, per share, today)</p>
          <dl className="tabular grid grid-cols-2 gap-4 text-sm sm:grid-cols-4">
            {[
              ["Delta", s.greeks.delta.toFixed(3), "value change per 1 move in the stock"],
              ["Gamma", s.greeks.gamma.toFixed(4), "change in delta per 1 move"],
              ["Theta", `${s.greeks.theta_per_day < 0 ? "−" : ""}${sym}${Math.abs(s.greeks.theta_per_day).toFixed(2)}`, "value change per day that passes"],
              ["Vega", `${s.greeks.vega_per_vol_point < 0 ? "−" : ""}${sym}${Math.abs(s.greeks.vega_per_vol_point).toFixed(2)}`, "value change per volatility point"],
            ].map(([label, value, help]) => (
              <div key={label}>
                <dt className="text-xs text-fg-subtle">{label}</dt>
                <dd className="font-semibold">{value}</dd>
                <dd className="text-[11px] text-fg-subtle">{help}</dd>
              </div>
            ))}
          </dl>
        </div>
        <div className="mt-4 overflow-x-auto">
          <table className="tabular w-full min-w-[420px] text-left text-sm">
            <caption className="sr-only">Legs of the structure</caption>
            <thead className="text-xs text-fg-subtle">
              <tr>{["Leg", "Strike", "Premium (est.)"].map((h) => <th key={h} scope="col" className="border-b border-border py-2 pr-4 font-medium">{h}</th>)}</tr>
            </thead>
            <tbody>
              {s.legs.map((l, i) => (
                <tr key={i}>
                  <td className="border-b border-border/60 py-2 pr-4 capitalize">{l.side} {l.kind}</td>
                  <td className="border-b border-border/60 py-2 pr-4">{l.strike === null ? "–" : `${sym}${l.strike}`}</td>
                  <td className="border-b border-border/60 py-2 pr-4">{sym}{l.premium_estimate.toFixed(2)}{l.kind === "stock" ? " (price)" : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <details className="mt-4 text-xs text-fg-muted">
          <summary className="cursor-pointer text-fg">Assumptions</summary>
          <ul className="mt-2 list-disc space-y-1 pl-5">{s.assumptions.map((a) => <li key={a}>{a}</li>)}</ul>
        </details>
      </Card>
    </div>
  );
}

function ResearchTab({ ticker }: { ticker: string }) {
  const name = COMPANY_NAMES[ticker] ?? ticker;
  const questions = [
    `How has ${ticker} performed over the last year, and what was its largest drawdown?`,
    `What is the current trading signal for ${ticker}, and why?`,
    `What is the latest news about ${name}?`,
  ];
  return (
    <div className="flex flex-col gap-4" data-loaded="research">
      <div className="max-w-2xl rounded-[var(--radius-card)] border border-border bg-surface-2 p-4 text-sm">
        <p className="font-medium text-fg">No filings for {name}</p>
        <p className="mt-1 text-fg-muted">
          The filings search covers only the annual 10-K reports of 15 US companies, such as NVIDIA and Apple.
          Indian companies do not file 10-Ks, so for {name} the research chat works from NSE prices and returns,
          the signal engine and news. Every figure in an answer is checked against what a tool returned.
        </p>
      </div>
      <p className="text-sm text-fg-muted">Questions the chat can answer from those tools:</p>
      <ul className="flex flex-col gap-2">
        {questions.map((q) => (
          <li key={q}>
            <Link
              href={`/research?q=${encodeURIComponent(q)}`}
              className="flex items-center gap-2 rounded-[var(--radius-control)] border border-border bg-surface px-4 py-3 text-sm text-fg hover:border-fg-subtle"
            >
              <MessageSquare className="size-4 shrink-0 text-accent" aria-hidden="true" />
              {q}
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

function StockPage() {
  const params = useParams<{ ticker: string }>();
  const ticker = decodeURIComponent(params.ticker);
  const search = useSearchParams();
  const router = useRouter();
  const { token, ready } = useAuth();
  const tab = (TABS as readonly string[]).includes(search.get("tab") ?? "") ? (search.get("tab") as Tab) : "chart";
  const [report, setReport] = useState<{ data?: SignalReport; error?: string } | null>(null);
  const slow = useSlow(report === null);
  const [chartReady, setChartReady] = useState(false);
  const onChartLoaded = useCallback(() => setChartReady(true), []);

  useEffect(() => {
    if (ready && !token) router.replace("/login");
  }, [ready, token, router]);

  useEffect(() => {
    if (!token) return;
    let live = true;
    api
      .signalReport(token, ticker)
      .then((d) => live && setReport({ data: d }))
      .catch((err) => live && setReport({ error: errorMessage(err) }));
    return () => {
      live = false;
    };
  }, [token, ticker]);

  const name = COMPANY_NAMES[ticker] ?? ticker;
  const ev = report?.data?.evidence;
  return (
    <main className="mx-auto flex max-w-6xl flex-col gap-6 px-4 py-8 sm:px-6">
      <Link href="/dashboard" className="inline-flex items-center gap-1 text-sm text-fg-muted hover:text-fg">
        <ArrowLeft className="size-4" aria-hidden="true" /> Watchlist
      </Link>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{name}</h1>
          <p className="text-sm text-fg-subtle">NSE: {shortTicker(ticker)}</p>
        </div>
        {ev?.ok ? <SignalBadge signal={ev.signal} coverage={ev.coverage.text} /> : null}
      </div>
      {slow && report === null ? <ColdStartNotice /> : null}
      {token ? (
        <Tabs value={tab} onValueChange={(v) => router.replace(`/stock/${encodeURIComponent(ticker)}?tab=${v}`, { scroll: false })}>
          <TabsList aria-label="Stock views">
            {TABS.map((t) => (
              <TabsTrigger key={t} value={t} className="capitalize">{t}</TabsTrigger>
            ))}
          </TabsList>
          <TabsContent value="chart">
            <div data-loaded={chartReady ? "chart" : undefined}>
              <PriceChart
                spec={{ kind: "price_chart", ticker, start: isoDaysAgo(730), end: isoDaysAgo(0), indicators: ["volume", "sma50", "sma200"] }}
                token={token}
                height={460}
                markers={report?.data?.markers ?? []}
                onLoaded={onChartLoaded}
              />
              <p className="text-xs text-fg-subtle">
                Arrows mark the days the signal went into the stock (green, below the bar) or out of it (red, above).
                Lines: 50-day and 200-day averages.
              </p>
            </div>
          </TabsContent>
          <TabsContent value="signal">
            {report?.error ? <ErrorState message={report.error} /> : report?.data ? <SignalTab report={report.data} token={token} ticker={ticker} /> : <TabSkeleton label="Loading the signal" />}
          </TabsContent>
          <TabsContent value="backtest">
            {report?.error ? <ErrorState message={report.error} /> : report?.data ? <BacktestTab report={report.data} /> : <TabSkeleton label="Loading the backtest" />}
          </TabsContent>
          <TabsContent value="options">
            <OptionsTab token={token} ticker={ticker} />
          </TabsContent>
          <TabsContent value="research">
            <ResearchTab ticker={ticker} />
          </TabsContent>
        </Tabs>
      ) : null}
    </main>
  );
}

export default function Page() {
  // useSearchParams needs a Suspense boundary in this Next.js version.
  return (
    <Suspense fallback={<main className="mx-auto max-w-6xl px-4 py-8"><TabSkeleton label="Loading" /></main>}>
      <StockPage />
    </Suspense>
  );
}
