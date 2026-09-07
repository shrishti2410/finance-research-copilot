"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { AuthCard } from "@/components/AuthCard";
import { useAuth } from "@/lib/auth";

export default function LoginPage() {
  const { login, token, ready, expired } = useAuth();
  const router = useRouter();
  const [pending, setPending] = useState(false);

  useEffect(() => {
    if (ready && token) router.replace("/");
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
      altHref="/signup"
      altPrompt="No account yet?"
      altLabel="Create one"
      onSubmit={async (email, password) => {
        setPending(true);
        try {
          await login(email, password);
          router.replace("/");
        } finally {
          setPending(false);
        }
      }}
    />
  );
}
