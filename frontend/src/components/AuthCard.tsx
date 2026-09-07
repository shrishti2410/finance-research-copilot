"use client";

import Link from "next/link";
import { useState } from "react";
import { errorMessage } from "@/lib/auth";

interface Props {
  heading: string;
  submitLabel: string;
  pending: boolean;
  hint?: string;
  minPasswordLength?: number;
  altHref: string;
  altPrompt: string;
  altLabel: string;
  onSubmit: (email: string, password: string) => Promise<void>;
}

/** The login and signup forms differ only in labels and which call they make. */
export function AuthCard({
  heading,
  submitLabel,
  pending,
  hint,
  minPasswordLength,
  altHref,
  altPrompt,
  altLabel,
  onSubmit,
}: Props) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <h1 className="text-lg font-medium">Finance Research Copilot</h1>
        <p className="mt-1 text-sm text-neutral-400">
          Grounded answers about public companies, with their sources.
        </p>

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
          <h2 className="text-sm font-medium text-neutral-300">{heading}</h2>

          <label className="block">
            <span className="text-xs text-neutral-400">Email</span>
            <input
              type="email"
              required
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="mt-1 w-full rounded-md border border-neutral-800 bg-neutral-900 px-3 py-2 text-sm outline-none focus:border-neutral-600"
            />
          </label>

          <label className="block">
            <span className="text-xs text-neutral-400">Password</span>
            <input
              type="password"
              required
              minLength={minPasswordLength}
              autoComplete={
                minPasswordLength ? "new-password" : "current-password"
              }
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1 w-full rounded-md border border-neutral-800 bg-neutral-900 px-3 py-2 text-sm outline-none focus:border-neutral-600"
            />
            {hint && (
              <span className="mt-1 block text-xs text-neutral-500">{hint}</span>
            )}
          </label>

          {error && (
            <p
              role="alert"
              className="rounded-md border border-red-900/60 bg-red-950/40 px-3 py-2 text-xs text-red-300"
            >
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={pending}
            className="w-full rounded-md bg-neutral-100 px-3 py-2 text-sm font-medium text-neutral-900 disabled:opacity-50"
          >
            {pending ? "Working…" : submitLabel}
          </button>
        </form>

        <p className="mt-6 text-xs text-neutral-500">
          {altPrompt}{" "}
          <Link href={altHref} className="text-neutral-300 underline">
            {altLabel}
          </Link>
        </p>
      </div>
    </main>
  );
}
