import type { Metadata } from "next";
import Link from "next/link";
import { ArrowRight, Briefcase, ExternalLink, FileText } from "lucide-react";

import { Card } from "@/components/ui/card";
import { AUTHOR } from "@/lib/site";

export const metadata: Metadata = {
  title: "About",
  description:
    "A free research site for public companies, with a strategy audit for Indian and US stocks: what it does, what it covers, where the data comes from, and who built it.",
};

// About the site, for a visitor: plain words, no technical detail and no
// measured figures (tests/test_readme_figures.py). How it is built, and every
// measured result, is in the README and benchmarks/.

function GitHubMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true" className={className}>
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
    </svg>
  );
}

const RESEARCH = {
  href: "/research",
  title: "Ask about a company's annual report",
  body: "Ask questions about 15 US companies and get answers from their 10-K filings, with the passages shown.",
};

// The strategy audit: everything about trading rules and prices.
const AUDIT = [
  {
    href: "/chart",
    title: "Charts and indicators",
    body: "Daily price charts with moving averages, RSI, MACD and Bollinger bands.",
  },
  {
    href: "/dashboard",
    title: "What classic trading rules say",
    body: "See what 15 classic trading rules (16 for US stocks) say about a stock today, and whether they have actually worked on it in the past.",
  },
  {
    href: "/backtest",
    title: "Backtests",
    body: "Test a trading rule on a stock's past prices, after trading costs, and compare it with simply holding the stock.",
  },
  {
    href: "/stock/SBIN.NS?tab=options",
    title: "Options payoffs",
    body: "See how common options strategies pay off at different prices, with their break-even points.",
  },
];

function Section({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
  return (
    <section aria-labelledby={id} className="mt-10">
      <h2 id={id} className="text-xl font-semibold tracking-tight text-fg">
        {title}
      </h2>
      <div className="mt-3 space-y-3 text-fg-muted">{children}</div>
    </section>
  );
}

// Only links the author has supplied (src/lib/site.ts); a missing one is not
// shown at all, never as a placeholder and never invented.
function ProfileLink({ href, label, icon }: { href: string; label: string; icon: React.ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className="flex items-center gap-3 rounded-[var(--radius-control)] border border-border bg-surface px-4 py-3 hover:border-fg-subtle"
    >
      <span className="text-accent">{icon}</span>
      <span className="font-medium text-fg">{label}</span>
      <ExternalLink className="ml-auto size-4 text-fg-subtle" aria-hidden="true" />
    </a>
  );
}

export default function About() {
  return (
    <main className="mx-auto max-w-3xl px-4 pb-6 pt-12 sm:px-6 sm:pt-16">
      <p className="text-sm font-medium text-accent">About</p>
      <h1 className="mt-2 text-3xl font-semibold tracking-tight text-fg sm:text-4xl">Finance Research Copilot</h1>

      <Section id="what" title="What this is">
        <p>
          A free research site for public companies. Its main part answers your questions about a company from
          its annual report. Alongside it, a strategy audit shows what popular trading rules say about Indian and
          US stocks, and whether they would really have worked, without having to take anyone&apos;s word for it.
        </p>
      </Section>

      <Section id="can-do" title="What you can do">
        <h3 className="text-sm font-semibold uppercase tracking-wider text-fg-subtle">Research</h3>
        <ul className="grid gap-3">
          {[RESEARCH].map(({ href, title, body }) => (
            <li key={href}>
              <Link
                href={href}
                className="flex h-full flex-col rounded-[var(--radius-card)] border border-border bg-surface p-4 hover:border-fg-subtle"
              >
                <span className="flex items-center gap-1 font-medium text-fg">
                  {title} <ArrowRight className="size-4 text-accent" aria-hidden="true" />
                </span>
                <span className="mt-1 text-sm">{body}</span>
              </Link>
            </li>
          ))}
        </ul>
        <h3 className="pt-3 text-sm font-semibold uppercase tracking-wider text-fg-subtle">Strategy audit</h3>
        <ul className="grid gap-3 sm:grid-cols-2">
          {AUDIT.map(({ href, title, body }) => (
            <li key={href}>
              <Link
                href={href}
                className="flex h-full flex-col rounded-[var(--radius-card)] border border-border bg-surface p-4 hover:border-fg-subtle"
              >
                <span className="flex items-center gap-1 font-medium text-fg">
                  {title} <ArrowRight className="size-4 text-accent" aria-hidden="true" />
                </span>
                <span className="mt-1 text-sm">{body}</span>
              </Link>
            </li>
          ))}
        </ul>
      </Section>

      <Section id="covered" title="What's covered">
        <p>
          <span className="font-medium text-fg">Charts, signals and backtests:</span> five Indian stocks listed on
          the NSE (Coforge, Dixon Technologies, State Bank of India, Premier Energies and HDFC Bank) and three US
          stocks (NVIDIA, Apple and Microsoft).
        </p>
        <p>
          <span className="font-medium text-fg">Annual-report questions:</span> 15 large US companies, including
          Apple, NVIDIA, Microsoft, Alphabet, Amazon, JPMorgan Chase and Coca-Cola.
        </p>
      </Section>

      <Section id="data" title="Data and freshness">
        <p>
          Company reports come from SEC EDGAR, the US regulator&apos;s public filing system. Prices are daily
          closing prices from Yahoo Finance, updated after each market close, so they are never live.
        </p>
      </Section>

      <Section id="principles" title="Principles">
        <ul className="list-disc space-y-2 pl-5">
          <li>Numbers come from the data, not from guesswork. If a figure can&apos;t be traced to its source, it is marked.</li>
          <li>
            Trading signals are shown with their real track record, including when they have no edge over simply
            holding the stock.
          </li>
          <li>Everything here is for learning. It is not investment advice, and it never tells you to buy or sell.</li>
        </ul>
      </Section>

      <Card className="mt-10 p-5">
        <h2 className="text-xl font-semibold tracking-tight text-fg">Built by {AUTHOR.name}</h2>
        <p className="mt-2 text-fg-muted">I designed and built this site on my own.</p>
        <div className="mt-4 grid gap-3 sm:grid-cols-3">
          {[
            { href: AUTHOR.github, label: "GitHub", icon: <GitHubMark className="size-5" /> },
            { href: AUTHOR.linkedin, label: "LinkedIn", icon: <Briefcase className="size-5" aria-hidden="true" /> },
            { href: AUTHOR.resume, label: "Resume", icon: <FileText className="size-5" aria-hidden="true" /> },
          ]
            .filter((l) => l.href)
            .map((l) => (
              <ProfileLink key={l.label} {...l} />
            ))}
        </div>
      </Card>
    </main>
  );
}
