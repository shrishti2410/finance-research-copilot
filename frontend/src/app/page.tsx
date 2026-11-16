import { BadgeCheck, Database, FileSearch, Scale, ShieldCheck, Sigma } from "lucide-react";

import { DemoButton } from "@/components/DemoButton";
import { GITHUB_URL } from "@/components/SiteChrome";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import metrics from "@/content/site-metrics.json";

const FEATURES = [
  {
    icon: ShieldCheck,
    title: "Every figure is checked",
    body: "A number in an answer must match something a tool returned. If it does not, the agent tries again or marks the answer unverified.",
  },
  {
    icon: Scale,
    title: "Reports, never advises",
    body: "A second check replaces any answer that tells you to buy, sell or hold with what the data shows.",
  },
  {
    icon: Sigma,
    title: "Backtests that can say no",
    body: "Walk-forward weights, Indian trading costs, random-timing baselines and hold-out stocks. The signal is shown with its verdict: no edge.",
  },
];

function Box({ title, items, tone = "default" }: { title: string; items: string[]; tone?: "default" | "accent" }) {
  return (
    <div
      className={
        "flex-1 rounded-[var(--radius-card)] border p-4 " +
        (tone === "accent" ? "border-accent/50 bg-surface-2" : "border-border bg-surface")
      }
    >
      <p className="text-sm font-semibold text-fg">{title}</p>
      <ul className="mt-2 space-y-1 text-xs text-fg-muted">
        {items.map((i) => (
          <li key={i}>{i}</li>
        ))}
      </ul>
    </div>
  );
}

function Arrow() {
  return (
    <div aria-hidden="true" className="flex items-center justify-center text-fg-subtle">
      <span className="hidden text-xl md:inline">→</span>
      <span className="text-xl md:hidden">↓</span>
    </div>
  );
}

export default function Landing() {
  return (
    <main className="mx-auto max-w-6xl px-4 sm:px-6">
      <section className="flex flex-col gap-6 pb-14 pt-16 sm:pt-24">
        <p className="text-sm font-medium text-accent">Python · FastAPI · Next.js · pgvector · LLM agents</p>
        <h1 className="max-w-3xl text-4xl font-semibold leading-tight tracking-tight text-fg sm:text-5xl">
          Grounded answers about public companies. Trading signals that admit when they don&apos;t work.
        </h1>
        <p className="max-w-2xl text-lg text-fg-muted">
          Ask about a company&apos;s annual report and get figures checked against the filing. Explore
          rule-based signals for five NSE stocks, explained from a strategy knowledge base and shown with
          their honest backtest results.
        </p>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start">
          <DemoButton />
          <Button asChild variant="secondary" size="lg">
            <a href={GITHUB_URL} target="_blank" rel="noreferrer noopener">
              View the code on GitHub
            </a>
          </Button>
        </div>
      </section>

      <section aria-labelledby="measured" className="pb-14">
        <h2 id="measured" className="mb-4 text-sm font-semibold uppercase tracking-wider text-fg-subtle">
          Measured, not claimed
        </h2>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {metrics.metrics.map((m) => (
            <Card key={m.label} className="flex flex-col gap-2 p-5">
              <p className="tabular text-3xl font-semibold tracking-tight text-fg">{m.value}</p>
              <p className="text-sm font-medium text-fg">{m.label}</p>
              <p className="text-xs text-fg-muted">{m.detail}</p>
              <p className="mt-auto pt-2 text-[11px] text-fg-subtle">Source: {m.source}</p>
            </Card>
          ))}
        </div>
      </section>

      <section aria-labelledby="how" className="pb-14">
        <h2 id="how" className="mb-4 text-sm font-semibold uppercase tracking-wider text-fg-subtle">
          How it works
        </h2>
        <div className="flex flex-col gap-3 md:flex-row md:items-stretch">
          <Box title="Browser" items={["Next.js app", "Dashboard, charts, research chat"]} />
          <Arrow />
          <Box
            title="API and agent"
            tone="accent"
            items={["FastAPI", "Agent loop with tool calls", "Grounding check on every figure", "Advice check on every answer"]}
          />
          <Arrow />
          <Box
            title="Tools and data"
            items={[
              "10-K search (pgvector)",
              "Reported figures (SEC XBRL)",
              "Daily prices, cached in Postgres",
              "Signal engine and backtester (Python)",
              "Strategy cards (pgvector)",
            ]}
          />
          <Arrow />
          <Box title="Language model" items={["Hosted: gpt-oss-120b", "Local: Qwen 2.5 on Ollama", "Explains what the tools return"]} />
        </div>
      </section>

      <section aria-label="Principles" className="grid grid-cols-1 gap-4 pb-6 md:grid-cols-3">
        {FEATURES.map(({ icon: Icon, title, body }) => (
          <Card key={title} className="p-5">
            <Icon className="size-5 text-accent" aria-hidden="true" />
            <p className="mt-3 font-medium text-fg">{title}</p>
            <p className="mt-1 text-sm text-fg-muted">{body}</p>
          </Card>
        ))}
      </section>

      <section className="flex flex-wrap gap-x-6 gap-y-2 pb-4 text-xs text-fg-subtle" aria-label="Stack">
        <span className="inline-flex items-center gap-1.5"><FileSearch className="size-3.5" aria-hidden="true" />RAG over SEC 10-K filings</span>
        <span className="inline-flex items-center gap-1.5"><Database className="size-3.5" aria-hidden="true" />Postgres with pgvector</span>
        <span className="inline-flex items-center gap-1.5"><BadgeCheck className="size-3.5" aria-hidden="true" />Tests and evals behind every claim</span>
      </section>
    </main>
  );
}
