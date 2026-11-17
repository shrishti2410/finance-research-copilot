"use client";

import { ArrowRight, Loader2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { ColdStartNotice, useSlow } from "@/components/states";
import { Button } from "@/components/ui/button";
import { errorMessage, useAuth } from "@/lib/auth";

/** Sign in to the shared demo account and open the dashboard. */
export function DemoButton() {
  const { token, demo } = useAuth();
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const slow = useSlow(busy);

  if (token) {
    return (
      <Button asChild size="lg">
        <Link href="/dashboard">
          Open the dashboard <ArrowRight className="size-4" aria-hidden="true" />
        </Link>
      </Button>
    );
  }
  return (
    <div className="flex flex-col gap-3">
      <Button
        size="lg"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setError(null);
          try {
            await demo();
            router.push("/dashboard");
          } catch (err) {
            setError(errorMessage(err));
            setBusy(false);
          }
        }}
      >
        {busy ? <Loader2 className="size-4 animate-spin" aria-hidden="true" /> : null}
        Try the demo
        {busy ? null : <ArrowRight className="size-4" aria-hidden="true" />}
      </Button>
      {slow ? <ColdStartNotice /> : null}
      {error ? (
        <p role="alert" className="text-sm text-down">
          {error}
        </p>
      ) : null}
    </div>
  );
}
