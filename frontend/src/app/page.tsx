import { ArrowRight, Bot, Calculator, ChevronDown, Database, FileSearch, Layers, LineChart, Sigma } from "lucide-react";
import Link from "next/link";

import { DemoButton } from "@/components/DemoButton";
import { BrowserFrame, Shot, type ShotName } from "@/components/landing/BrowserFrame";
import { MarketStrip } from "@/components/landing/MarketStrip";
import { ProductTour } from "@/components/landing/ProductTour";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { AUTHOR, GITHUB_URL } from "@/lib/site";
import { loadStrip } from "@/lib/strip";

// No figure on this page is a measurement. Measured results live in the
// README and benchmarks/ only, each quoted from its record and checked by
// tests/test_readme_figures.py. The counts here (8 stocks, 15 companies) are
// what the deployment covers.

const FEATURES: { icon: typeof FileSearch; title: string; body: string; shot: ShotName }[] = [
  {
    icon: FileSearch,
    title: "Ask the filings",
    body: "Questions about 15 US companies, answered from their 10-K reports and reported figures, with the passages cited.",
    shot: "feature-research",
  },
  {
    icon: Layers,
    title: "Signals with reasons",
    body: "Textbook rules vote on each stock. Every vote is shown with its weight and a handbook card explaining the rule.",
    shot: "feature-signal",
  },
  {
    icon: Sigma,
    title: "Honest backtests",
    body: "Walk-forward, after trading costs, against buy-and-hold and random timing. When there is no edge, it says so.",
    shot: "feature-backtest",
  },
  {
    icon: LineChart,
    title: "Options analytics",
    body: "Payoffs, breakevens and Greeks for common structures, with premiums estimated from historical volatility.",
    shot: "feature-options",
  },
];

const STEPS = [
  {
    icon: Database,
    title: "Data",
    body: "SEC 10-K filings and their reported figures, and daily prices for five NSE and three US stocks.",
  },
  {
    icon: Calculator,
    title: "Python computes",
    body: "Indicators, signals, backtests, option payoffs, and every ratio or growth rate in an answer are computed in code, not by the model.",
  },
  {
    icon: Bot,
    title: "AI explains, a checker verifies",
    body: "A language model explains what the tools returned. A checker flags any figure no tool produced, and replaces any advice to buy or sell.",
  },
];

const FAQ = [
  {
    q: "Is this investment advice?",
    a: "No. It is an educational project. It reports what filings and past prices show, and a guard replaces any answer that tells you to buy, sell or hold. Signals are a vote of textbook rules computed from past prices, not forecasts, and the published backtests found no edge over buy-and-hold.",
  },
  {
    q: "Where does the data come from?",
    a: "Annual 10-K reports and XBRL reported figures from SEC EDGAR, and daily split-adjusted prices from Yahoo Finance. Option premiums are estimates from historical volatility, not market prices.",
  },
  {
    q: "Why is the first load sometimes slow?",
    a: "The API runs on a free hosting tier that sleeps after 15 minutes without traffic. The first request after that has to wake it, and the page shows a notice while it waits. Once it is awake, stock pages come from a copy computed once a day after the market close, so they do not wait for the calculations.",
  },
  {
    q: "Which stocks are covered?",
    a: "Signals cover five NSE stocks (Coforge, Dixon Technologies, State Bank of India, Premier Energies, HDFC Bank) and three US stocks (NVIDIA, Apple, Microsoft). The filings search covers the 10-K reports of 15 US companies.",
  },
];

// Served from Vercel's cache and regenerated at most every 15 minutes
// (src/lib/strip.ts), so the page and its market strip load while the API sleeps.
export const revalidate = 900;

export default async function Landing() {
  const strip = await loadStrip();
  return (
    <main>
      <section className="mx-auto grid max-w-6xl items-center gap-10 px-4 pb-12 pt-12 sm:px-6 sm:pt-20 lg:grid-cols-[1fr_1.15fr]">
        <div className="fade-up flex flex-col gap-6">
          <h1 className="text-4xl font-semibold leading-[1.1] tracking-tight text-fg sm:text-5xl">
            Research companies and test trading strategies, with every number checked
          </h1>
          <p className="max-w-xl text-lg text-fg-muted">
            Ask questions answered from SEC filings, explore rule-based signals for Indian and US stocks, and
            backtest them honestly. Python does the maths, the AI explains it, and a checker verifies each
            figure.
          </p>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start">
            <DemoButton />
            <Button asChild variant="secondary" size="lg">
              <a href="#how-it-works">
                See how it works <ChevronDown className="size-4" aria-hidden="true" />
              </a>
            </Button>
          </div>
        </div>
        <BrowserFrame
          shot="hero-dashboard"
          priority
          sizes="(min-width: 1024px) 600px, 100vw"
          className="fade-up [animation-delay:120ms]"
        />
      </section>

      <MarketStrip initial={strip} />

      <section aria-labelledby="features" className="mx-auto max-w-6xl px-4 pt-16 sm:px-6">
        <h2 id="features" className="text-2xl font-semibold tracking-tight text-fg">
          Filings, signals and backtests in one place
        </h2>
        <div className="mt-6 grid grid-cols-1 gap-4 md:grid-cols-2">
          {FEATURES.map(({ icon: Icon, title, body, shot }) => (
            <Card key={title} className="flex flex-col gap-3 p-5 transition-colors hover:border-fg-subtle">
              <div className="flex items-center gap-2">
                <Icon className="size-5 text-accent" aria-hidden="true" />
                <p className="font-medium text-fg">{title}</p>
              </div>
              <p className="text-sm text-fg-muted">{body}</p>
              <div className="mt-auto max-h-80 overflow-hidden rounded-md border border-border">
                <Shot shot={shot} sizes="(min-width: 1152px) 520px, (min-width: 768px) 45vw, 92vw" className="rounded-none border-0" />
              </div>
            </Card>
          ))}
        </div>
      </section>

      <section aria-labelledby="tour" className="mx-auto max-w-6xl px-4 pt-20 sm:px-6">
        <h2 id="tour" className="text-2xl font-semibold tracking-tight text-fg">
          A stock page, tab by tab
        </h2>
        <p className="mb-6 mt-2 text-fg-muted">Screenshots of the live pages for State Bank of India and NVIDIA.</p>
        <ProductTour />
      </section>

      <section aria-labelledby="how-it-works" className="mx-auto max-w-6xl scroll-mt-20 px-4 pt-20 sm:px-6">
        <h2 id="how-it-works" className="text-2xl font-semibold tracking-tight text-fg">
          How it works
        </h2>
        <ol className="mt-6 grid grid-cols-1 gap-4 md:grid-cols-3">
          {STEPS.map(({ icon: Icon, title, body }, i) => (
            <li key={title}>
              <Card className="h-full p-5">
                <div className="flex items-center gap-3">
                  <span className="tabular grid size-7 place-items-center rounded-full border border-border text-xs text-fg-muted">
                    {i + 1}
                  </span>
                  <Icon className="size-5 text-accent" aria-hidden="true" />
                </div>
                <p className="mt-3 font-medium text-fg">{title}</p>
                <p className="mt-1 text-sm text-fg-muted">{body}</p>
              </Card>
            </li>
          ))}
        </ol>
      </section>

      <section aria-labelledby="faq" className="mx-auto max-w-3xl px-4 pt-20 sm:px-6">
        <h2 id="faq" className="text-2xl font-semibold tracking-tight text-fg">
          Questions
        </h2>
        <div className="mt-6 divide-y divide-border rounded-[var(--radius-card)] border border-border bg-surface">
          {FAQ.map(({ q, a }) => (
            <details key={q} className="group px-5 py-4">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-4 font-medium text-fg">
                {q}
                <ChevronDown
                  className="size-4 shrink-0 text-fg-subtle transition-transform group-open:rotate-180"
                  aria-hidden="true"
                />
              </summary>
              <p className="mt-3 text-sm text-fg-muted">{a}</p>
            </details>
          ))}
        </div>
      </section>

      <section aria-labelledby="built-by" className="mx-auto max-w-3xl px-4 pt-20 sm:px-6">
        <div className="flex flex-col gap-3 rounded-[var(--radius-card)] border border-border bg-surface p-6 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 id="built-by" className="text-lg font-semibold text-fg">
              Built by {AUTHOR.name}
            </h2>
            <p className="mt-1 text-sm text-fg-muted">
              Designed and built end to end: the Python agent and signal engine, the data pipeline, and this site.
            </p>
          </div>
          <Link href="/about" className="flex shrink-0 items-center gap-1 text-sm text-accent hover:underline">
            About the author <ArrowRight className="size-4" aria-hidden="true" />
          </Link>
        </div>
      </section>

      <section className="mx-auto flex max-w-6xl flex-col items-center gap-4 px-4 pt-20 text-center sm:px-6">
        <h2 className="text-2xl font-semibold tracking-tight text-fg">See it on real data</h2>
        <p className="max-w-xl text-fg-muted">The demo needs no sign-up. It opens the watchlist with today&apos;s signals.</p>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start">
          <DemoButton />
          <Button asChild variant="secondary" size="lg">
            <a href={GITHUB_URL} target="_blank" rel="noreferrer noopener">
              View the code
            </a>
          </Button>
        </div>
      </section>
    </main>
  );
}
