"use client";

// Loading, waking, empty and error states, one look for all pages.
import { AlertTriangle, Coffee, Inbox } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { useAuth } from "@/lib/auth";

/**
 * True once `active` has stayed true for `afterMs`. The API runs on a free
 * host that sleeps when idle; the first request can take most of a minute,
 * and without a word the page just looks broken.
 */
export function useSlow(active: boolean, afterMs = 4000): boolean {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (!active) return;
    const id = setTimeout(() => setSlow(true), afterMs);
    return () => {
      clearTimeout(id);
      setSlow(false);
    };
  }, [active, afterMs]);
  return active && slow;
}

export function ColdStartNotice() {
  return (
    <div
      role="status"
      className="flex items-start gap-3 rounded-[var(--radius-card)] border border-border bg-surface-2 p-4 text-sm text-fg-muted"
    >
      <Coffee className="mt-0.5 size-4 shrink-0 text-accent" aria-hidden="true" />
      <p>
        <span className="font-medium text-fg">Waking the API.</span> It runs on free hosting that
        sleeps when nobody is using it, so the first request can take up to a minute. Later ones are
        quick.
      </p>
    </div>
  );
}

/** The cold-start notice while the stored session's check is being retried
 * (the API is asleep or busy). Shown once, under the header, on any page. */
export function SessionWakingNotice() {
  const { waking } = useAuth();
  if (!waking) return null;
  return (
    <div className="mx-auto max-w-6xl px-4 pt-4 sm:px-6">
      <ColdStartNotice />
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div
      role="alert"
      className="flex flex-col items-start gap-3 rounded-[var(--radius-card)] border border-border bg-surface p-5 text-sm"
    >
      <div className="flex items-center gap-2 font-medium text-fg">
        <AlertTriangle className="size-4 text-warn" aria-hidden="true" />
        Something went wrong
      </div>
      <p className="text-fg-muted">{message}</p>
      {onRetry ? (
        <Button variant="secondary" size="sm" onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-[var(--radius-card)] border border-dashed border-border p-8 text-center text-sm text-fg-muted">
      <Inbox className="size-5 text-fg-subtle" aria-hidden="true" />
      <p className="font-medium text-fg">{title}</p>
      {children}
    </div>
  );
}
