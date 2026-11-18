"use client";

import { LineChart, Menu, X } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";

import { DemoButton } from "@/components/DemoButton";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";

import { GITHUB_URL } from "@/lib/site";

export { GITHUB_URL };

function GitHubMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true" className={className}>
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
    </svg>
  );
}

const NAV = [
  { href: "/research", label: "Research" },
  { href: "/dashboard", label: "Signals" },
];

export function SiteHeader() {
  const { token, logout } = useAuth();
  const path = usePathname();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  // Close the phone menu whenever the page changes.
  const [menuPath, setMenuPath] = useState(path);
  if (menuPath !== path) {
    setMenuPath(path);
    setOpen(false);
  }

  const links = NAV.map((item) => (
    <Link
      key={item.href}
      href={item.href}
      aria-current={path?.startsWith(item.href) ? "page" : undefined}
      className={cn(
        "rounded-md px-2.5 py-1.5 text-sm text-fg-muted hover:text-fg",
        path?.startsWith(item.href) && "bg-surface-2 text-fg",
      )}
    >
      {item.label}
    </Link>
  ));
  const account = token ? (
    <Button
      variant="ghost"
      size="sm"
      onClick={() => {
        logout();
        router.replace("/");
      }}
    >
      Sign out
    </Button>
  ) : (
    <Button asChild variant="ghost" size="sm">
      <Link href="/login">Sign in</Link>
    </Button>
  );

  return (
    <header className="sticky top-0 z-30 border-b border-border bg-bg/85 backdrop-blur">
      <div className="mx-auto flex h-14 max-w-6xl items-center gap-2 px-4 sm:gap-4 sm:px-6">
        <Link href="/" className="flex items-center gap-2 font-semibold text-fg">
          <span className="grid size-7 place-items-center rounded-md bg-accent-strong text-bg">
            <LineChart className="size-4" aria-hidden="true" />
          </span>
          <span className="hidden lg:inline">Finance Research Copilot</span>
          <span className="lg:hidden">FRC</span>
        </Link>
        <nav aria-label="Main" className="hidden items-center gap-1 md:flex">
          {links}
        </nav>
        <div className="ml-auto flex items-center gap-1 sm:gap-2">
          <a
            href={GITHUB_URL}
            target="_blank"
            rel="noreferrer noopener"
            className="hidden rounded-md p-2 text-fg-muted hover:text-fg md:block"
            aria-label="Source code on GitHub"
          >
            <GitHubMark className="size-5" />
          </a>
          <div className="hidden md:block">{account}</div>
          <DemoButton compact />
          <button
            type="button"
            className="rounded-md p-2 text-fg-muted hover:text-fg md:hidden"
            aria-label={open ? "Close the menu" : "Open the menu"}
            aria-expanded={open}
            aria-controls="phone-menu"
            onClick={() => setOpen((o) => !o)}
          >
            {open ? <X className="size-5" aria-hidden="true" /> : <Menu className="size-5" aria-hidden="true" />}
          </button>
        </div>
      </div>
      {open ? (
        <nav id="phone-menu" aria-label="Main" className="border-t border-border px-4 py-3 md:hidden">
          <div className="flex flex-col gap-1">
            {links}
            <a
              href={GITHUB_URL}
              target="_blank"
              rel="noreferrer noopener"
              className="flex items-center gap-2 rounded-md px-2.5 py-1.5 text-sm text-fg-muted hover:text-fg"
            >
              <GitHubMark className="size-4" /> GitHub
            </a>
            <div className="pt-1">{account}</div>
          </div>
        </nav>
      ) : null}
    </header>
  );
}

const FOOTER_LINKS: { title: string; links: { label: string; href: string; external?: boolean }[] }[] = [
  {
    title: "Product",
    links: [
      { label: "Research", href: "/research" },
      { label: "Signals", href: "/dashboard" },
      { label: "Backtests", href: "/backtest" },
      { label: "About", href: "/about" },
    ],
  },
  {
    title: "Source",
    links: [
      { label: "GitHub", href: GITHUB_URL, external: true },
      { label: "Benchmark records", href: `${GITHUB_URL}/tree/master/benchmarks`, external: true },
      { label: "Known limitations", href: `${GITHUB_URL}/blob/master/docs/KNOWN_LIMITATIONS.md`, external: true },
    ],
  },
  {
    title: "Data",
    links: [
      { label: "SEC EDGAR (10-K filings, XBRL)", href: "https://www.sec.gov/edgar/search/", external: true },
      { label: "Yahoo Finance (daily prices)", href: "https://finance.yahoo.com/", external: true },
    ],
  },
];

export function SiteFooter() {
  return (
    <footer className="mt-16 border-t border-border">
      <div className="mx-auto grid max-w-6xl gap-8 px-4 py-10 text-sm sm:px-6 md:grid-cols-[1.4fr_1fr_1fr_1.2fr]">
        <div className="flex flex-col gap-2">
          <p className="flex items-center gap-2 font-semibold text-fg">
            <span className="grid size-6 place-items-center rounded-md bg-accent-strong text-bg">
              <LineChart className="size-3.5" aria-hidden="true" />
            </span>
            Finance Research Copilot
          </p>
          <p className="text-xs text-fg-muted">
            An educational project. Nothing here is investment advice: the site reports what filings and
            past prices show, and never tells you to buy or sell.
          </p>
        </div>
        {FOOTER_LINKS.map((col) => (
          <div key={col.title}>
            <p className="text-xs font-semibold uppercase tracking-wider text-fg-subtle">{col.title}</p>
            <ul className="mt-3 flex flex-col gap-2">
              {col.links.map((l) => (
                <li key={l.label}>
                  {l.external ? (
                    <a href={l.href} target="_blank" rel="noreferrer noopener" className="text-fg-muted hover:text-fg">
                      {l.label}
                    </a>
                  ) : (
                    <Link href={l.href} className="text-fg-muted hover:text-fg">
                      {l.label}
                    </Link>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </footer>
  );
}
