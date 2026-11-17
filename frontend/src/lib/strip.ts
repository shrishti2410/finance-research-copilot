// The landing page's market strip, fetched on the server for the cached
// (ISR) page, so it shows even while the free API is asleep.
//
// The page is regenerated at most every REVALIDATE_SECONDS. If the API does not
// answer during a regeneration, this throws on purpose: Next.js then keeps
// serving the last page it built, strip and its close dates included, and tries
// again on a later request (the "ISR" guide in node_modules/next/dist/docs).
// Only at build time does a failure return null instead, so a sleeping API
// cannot fail a deploy; the browser then fetches the strip as before.

import { API_BASE } from "@/lib/api";

export type StripEntry = {
  ticker: string;
  exchange: string | null;
  kind: "index" | "stock";
  currency_symbol: string | null;
  price: number;
  change_pct: number | null;
  as_of: string | null;
};

export const REVALIDATE_SECONDS = 900;
const TIMEOUT_MS = 10_000;

export async function loadStrip(): Promise<StripEntry[] | null> {
  try {
    const res = await fetch(`${API_BASE}/market/strip`, {
      signal: AbortSignal.timeout(TIMEOUT_MS),
      next: { revalidate: REVALIDATE_SECONDS },
    });
    if (!res.ok) throw new Error(`market strip: HTTP ${res.status}`);
    const body = await res.json();
    if (!Array.isArray(body?.entries) || !body.entries.length) throw new Error("market strip: empty");
    return body.entries as StripEntry[];
  } catch (err) {
    if (process.env.NEXT_PHASE === "phase-production-build") return null;
    throw err;
  }
}
