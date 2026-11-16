"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { AuthCard } from "@/components/AuthCard";
import { api, type AuthOptions } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export default function LoginPage() {
  const { login, demo, token, ready, expired } = useAuth();
  const router = useRouter();
  const [pending, setPending] = useState(false);
  // What the deployment offers. Until it is known, offer what a development
  // server always has: sign-in and signup, no demo.
  const [options, setOptions] = useState<AuthOptions | null>(null);

  useEffect(() => {
    api.options().then(setOptions).catch(() => setOptions(null));
  }, []);

  useEffect(() => {
    if (ready && token) router.replace("/dashboard");
  }, [ready, token, router]);

  return (
    <AuthCard
      heading="Sign in"
      submitLabel="Sign in"
      // Sessions last 30 minutes. Arriving here because one ran out looks
      // identical to arriving here deliberately unless the page says so.
      notice={
        expired
          ? "Your session expired. Please sign in again -- your conversations are still there."
          : undefined
      }
      pending={pending}
      altHref={options?.signup === false ? undefined : "/signup"}
      altPrompt="No account yet?"
      altLabel="Create one"
      demo={
        options?.demo
          ? {
              label: "Try the demo",
              note:
                (options.demo_questions_per_account
                  ? `A fresh account, ${options.demo_questions_per_account} questions a day. ` +
                    "Nothing you ask is visible to other visitors. "
                  : "") +
                // The free API host sleeps when idle and takes about a minute
                // to wake (docs/DEPLOY.md), which otherwise looks like a hang.
                "The demo runs on free hosting that sleeps when nobody is using it, so " +
                "the first request after a quiet spell can take a minute or so.",
              onClick: async () => {
                setPending(true);
                try {
                  await demo();
                  router.replace("/dashboard");
                } finally {
                  setPending(false);
                }
              },
            }
          : undefined
      }
      onSubmit={async (email, password) => {
        setPending(true);
        try {
          await login(email, password);
          router.replace("/dashboard");
        } finally {
          setPending(false);
        }
      }}
    />
  );
}
