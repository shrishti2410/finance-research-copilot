import type { Metadata } from "next";
import { Briefcase, ExternalLink, FileText } from "lucide-react";

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

      <Card className="mt-8 p-5">
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
