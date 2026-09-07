"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { AuthCard } from "@/components/AuthCard";
import { useAuth } from "@/lib/auth";

export default function SignupPage() {
  const { signup, token, ready } = useAuth();
  const router = useRouter();
  const [pending, setPending] = useState(false);

  useEffect(() => {
    if (ready && token) router.replace("/");
  }, [ready, token, router]);

  return (
    <AuthCard
      heading="Create an account"
      submitLabel="Sign up"
      pending={pending}
      // The backend enforces this; stating it up front beats a 422 after the
      // fact, and the message here is the same rule, not a second one.
      hint="At least 8 characters."
      minPasswordLength={8}
      altHref="/login"
      altPrompt="Already have an account?"
      altLabel="Sign in"
      onSubmit={async (email, password) => {
        setPending(true);
        try {
          await signup(email, password);
          router.replace("/");
        } finally {
          setPending(false);
        }
      }}
    />
  );
}
