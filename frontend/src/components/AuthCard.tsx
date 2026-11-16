"use client";

import Link from "next/link";
import { useState } from "react";
import { errorMessage } from "@/lib/auth";

interface Props {
  heading: string;
  submitLabel: string;
  pending: boolean;
  /** Why the user is looking at this page, when it was not their idea. */
  notice?: string;
  hint?: string;
  minPasswordLength?: number;
  /** The link to the other form. Omitted when that form is closed. */
  altHref?: string;
  altPrompt?: string;
  altLabel?: string;
  onSubmit: (email: string, password: string) => Promise<void>;
  /** Shown above the form when the deployment offers a demo account. */
  demo?: { label: string; note?: string; onClick: () => Promise<void> };
}

/** The login and signup forms differ only in labels and which call they make. */
export function AuthCard({
  heading,
  submitLabel,
  pending,
  notice,
  hint,
  minPasswordLength,
  altHref,
  altPrompt,
  altLabel,
  onSubmit,
  demo,
}: Props) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <h1 className="text-lg font-medium">Finance Research Copilot</h1>
        <p className="mt-1 text-sm text-fg-muted">
          Grounded answers about public companies, with their sources.
        </p>

        {demo && (
          <div className="mt-8 space-y-2">
            <button
              type="button"
              disabled={pending}
              onClick={async () => {
                setError(null);
                try {
                  await demo.onClick();
                } catch (caught) {
                  setError(errorMessage(caught));
                }
              }}
              className="w-full rounded-md border border-fg-subtle px-3 py-2 text-sm font-medium text-fg disabled:opacity-50"
            >
              {demo.label}
            </button>
            {demo.note && <p className="text-xs text-fg-subtle">{demo.note}</p>}
          </div>
        )}

        <form
          className="mt-8 space-y-4"
          onSubmit={async (event) => {
            event.preventDefault();
            setError(null);
            try {
              await onSubmit(email, password);
            } catch (caught) {
              setError(errorMessage(caught));
            }
          }}
        >
          <h2 className="text-sm font-medium text-fg-muted">{heading}</h2>

          {notice && (
            <p
              role="status"
              className="rounded-md border border-warn/40 bg-surface-2 px-3 py-2 text-xs text-warn"
            >
              {notice}
            </p>
          )}

          <label className="block">
            <span className="text-xs text-fg-muted">Email</span>
            <input
              type="email"
              required
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="mt-1 w-full rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
            />
          </label>

          <label className="block">
            <span className="text-xs text-fg-muted">Password</span>
            <input
              type="password"
              required
              minLength={minPasswordLength}
              autoComplete={
                minPasswordLength ? "new-password" : "current-password"
              }
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1 w-full rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
            />
            {hint && (
              <span className="mt-1 block text-xs text-fg-subtle">{hint}</span>
            )}
          </label>

          {error && (
            <p
              role="alert"
              className="rounded-md border border-down/40 bg-down-soft px-3 py-2 text-xs text-down"
            >
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={pending}
            className="w-full rounded-md bg-accent-strong px-3 py-2 text-sm font-medium text-bg disabled:opacity-50"
          >
            {pending ? "Working…" : submitLabel}
          </button>
        </form>

        {altHref && (
          <p className="mt-6 text-xs text-fg-subtle">
            {altPrompt}{" "}
            <Link href={altHref} className="text-fg-muted underline">
              {altLabel}
            </Link>
          </p>
        )}
      </div>
    </main>
  );
}
