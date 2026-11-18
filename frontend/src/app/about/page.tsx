import type { Metadata } from "next";
import { Briefcase, ExternalLink, FileText, Scale, ShieldCheck, Sigma } from "lucide-react";

import { Card } from "@/components/ui/card";
import { AUTHOR, GITHUB_URL } from "@/lib/site";

export const metadata: Metadata = {
  title: "About",
  description: `Who built Finance Research Copilot, and how to reach ${AUTHOR.name}.`,
};

function GitHubMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true" className={className}>
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
    </svg>
  );
}

// "How it's built": the architecture and the guards, moved here from the
// removed methodology page. No figures: every measured result is in the
// README and benchmarks/, quoted from its record.
const GUARDS = [
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

// The boxes stack at every width, so the arrow always points down.
function Arrow() {
  return (
    <div aria-hidden="true" className="flex items-center justify-center text-fg-subtle">
      <span className="text-xl">↓</span>
    </div>
  );
}

// A link the author has not supplied yet is shown as "coming soon", never as
// an invented address (src/lib/site.ts).
function ProfileLink({ href, label, icon }: { href: string; label: string; icon: React.ReactNode }) {
  const body = (
    <>
      <span className="text-accent">{icon}</span>
      <span className="font-medium text-fg">{label}</span>
      {href ? <ExternalLink className="ml-auto size-4 text-fg-subtle" aria-hidden="true" /> : (
        <span className="ml-auto text-xs text-fg-subtle">coming soon</span>
      )}
    </>
  );
  const box = "flex items-center gap-3 rounded-[var(--radius-control)] border border-border bg-surface px-4 py-3";
  return href ? (
    <a href={href} target="_blank" rel="noreferrer noopener" className={`${box} hover:border-fg-subtle`}>
      {body}
    </a>
  ) : (
    <div className={box} aria-disabled="true">
      {body}
    </div>
  );
}

export default function About() {
  return (
    <main className="mx-auto max-w-3xl px-4 pb-6 pt-12 sm:px-6 sm:pt-16">
      <p className="text-sm font-medium text-accent">About</p>
      <h1 className="mt-2 text-3xl font-semibold tracking-tight text-fg sm:text-4xl">Built by {AUTHOR.name}</h1>
      <div className="mt-6 space-y-4 text-fg-muted">
        <p>
          Finance Research Copilot is a project I designed and built end to end: a FastAPI service and an agent
          loop in Python, Postgres with pgvector, a signal engine and backtester, and this Next.js front end.
        </p>
        <p>
          The rule I held it to is that every number is computed in code and checked, and every measured claim
          comes from a committed record. Where a result is negative, as the trading signal&apos;s is, the site
          says so.
        </p>
      </div>

      <section aria-labelledby="how-built" className="mt-10">
        <h2 id="how-built" className="text-xl font-semibold tracking-tight text-fg">
          How it&apos;s built
        </h2>
        <div className="mt-4 flex flex-col gap-3">
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
        <h3 className="mt-8 text-sm font-semibold uppercase tracking-wider text-fg-subtle">Guards</h3>
        <div className="mt-3 grid grid-cols-1 gap-4 sm:grid-cols-3">
          {GUARDS.map(({ icon: Icon, title, body }) => (
            <Card key={title} className="p-5">
              <Icon className="size-5 text-accent" aria-hidden="true" />
              <p className="mt-3 font-medium text-fg">{title}</p>
              <p className="mt-1 text-sm text-fg-muted">{body}</p>
            </Card>
          ))}
        </div>
        <p className="mt-4 text-sm text-fg-muted">
          Measured results, each with the command and commit that produced it, are in the{" "}
          <a href={`${GITHUB_URL}#readme`} target="_blank" rel="noreferrer noopener" className="text-accent underline">
            README
          </a>{" "}
          and the{" "}
          <a href={`${GITHUB_URL}/tree/master/benchmarks`} target="_blank" rel="noreferrer noopener" className="text-accent underline">
            benchmark records
          </a>
          .
        </p>
      </section>

      <Card className="mt-10 p-5">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-fg-subtle">Find me</h2>
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <ProfileLink href={AUTHOR.github} label="GitHub" icon={<GitHubMark className="size-5" />} />
          <ProfileLink href={AUTHOR.linkedin} label="LinkedIn" icon={<Briefcase className="size-5" aria-hidden="true" />} />
          <ProfileLink href={AUTHOR.resume} label="Resume" icon={<FileText className="size-5" aria-hidden="true" />} />
          <ProfileLink href={GITHUB_URL} label="This project's source" icon={<GitHubMark className="size-5" />} />
        </div>
      </Card>
    </main>
  );
}
