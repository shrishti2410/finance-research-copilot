"use client";

import { ArrowRight, Loader2 } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";

import { ColdStartNotice, useSlow } from "@/components/states";
import { Button } from "@/components/ui/button";
import { errorMessage, useAuth } from "@/lib/auth";

/** Sign in to the shared demo account and open Research, where the example
 * questions are (research is the main product, 2026-11-18). `compact` is
 * the header's version: small, and the slow-start notice is left to the
 * spinner (there is no room for it under a header button). */
export function DemoButton({ compact = false }: { compact?: boolean }) {
  const { token, demo } = useAuth();
  const router = useRouter();
  const path = usePathname();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const slow = useSlow(busy);

  if (token) {
    // The header's button never repeats the nav's Research tab: on /research it
    // starts a new question, elsewhere it opens the dashboard.
    const onResearch = path?.startsWith("/research") ?? false;
    const [href, label] = compact
      ? onResearch
        ? ["/research?new=1", "New question"]
        : ["/dashboard", "Dashboard"]
      : ["/research", "Open Research"];
    return (
      <Button asChild size={compact ? "sm" : "lg"}>
        <Link href={href}>
          {label} <ArrowRight className="size-4" aria-hidden="true" />
        </Link>
      </Button>
    );
  }
  return (
    <div className={compact ? "flex" : "flex flex-col gap-3"}>
      <Button
        size={compact ? "sm" : "lg"}
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setError(null);
          try {
            await demo();
            router.push("/research");
          } catch (err) {
            // The header has no room for the message: the sign-in page shows
            // its own, with the demo button there to retry.
            if (compact) router.push("/login");
            setError(errorMessage(err));
            setBusy(false);
          }
        }}
      >
        {busy ? <Loader2 className="size-4 animate-spin" aria-hidden="true" /> : null}
        Try the demo
        {busy ? null : <ArrowRight className="size-4" aria-hidden="true" />}
      </Button>
      {slow && !compact ? <ColdStartNotice /> : null}
      {error && !compact ? (
        <p role="alert" className="text-sm text-down">
          {error}
        </p>
      ) : null}
    </div>
  );
}
