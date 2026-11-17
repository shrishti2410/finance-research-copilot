// Checking a stored token on page load, without signing the user out when the
// API is merely asleep or busy.
//
// Plain JavaScript (an ES module with JSDoc types) so that its tests run on
// any Node with `node --test`, with no TypeScript step: see
// frontend/tests/sessionCheck.test.mjs and tests/test_frontend_logic.py.
//
// Only a 401 means the token is bad. The free API sleeps when idle, answers
// 429 when a visitor reloads fast, and can time out or answer 5xx while it
// wakes; none of those says anything about the token, so the session is kept
// and the check is retried with backoff.

/** Waits between attempts: about 75 s in all, enough for a cold start. */
export const RETRY_DELAYS_MS = [1000, 2000, 4000, 8000, 15000, 15000, 15000, 15000];

/** How long one /auth/me attempt may take before it counts as a timeout. */
export const ATTEMPT_TIMEOUT_MS = 20000;

/**
 * What a failed /auth/me says about the stored token.
 *   "invalid": 401, the token is bad; drop it.
 *   "retry":   no answer (network error or timeout: status 0), 408, 429 or 5xx;
 *              the API is asleep or busy, try again.
 *   "keep":    any other status; not a verdict on the token, keep it.
 * @param {unknown} error
 * @returns {"invalid" | "retry" | "keep"}
 */
export function classify(error) {
  const status =
    typeof error === "object" && error !== null && typeof (/** @type {{status?: unknown}} */ (error).status) === "number"
      ? /** @type {{status: number}} */ (error).status
      : 0;
  if (status === 401) return "invalid";
  if (status === 0 || status === 408 || status === 429 || status >= 500) return "retry";
  return "keep";
}

/**
 * @template U
 * @typedef {{ outcome: "valid", user: U } | { outcome: "invalid" } | { outcome: "unverified" }} SessionCheck
 *   "unverified": the API never gave a verdict; keep the token and carry on.
 *   Any request that later gets a 401 still ends the session.
 */

/**
 * Check a stored token, retrying while the API is asleep or busy.
 * @template U
 * @param {() => Promise<U>} check  one /auth/me attempt
 * @param {{
 *   delays?: number[],
 *   sleep?: (ms: number) => Promise<void>,
 *   onWaiting?: (attempt: number) => void,
 *   isAlive?: () => boolean,
 * }} [options]
 *   onWaiting: called before each retry (show the "waking up" notice).
 *   isAlive:   false once the page has gone away; stops retrying.
 * @returns {Promise<SessionCheck<U>>}
 */
export async function checkStoredSession(check, options = {}) {
  const {
    delays = RETRY_DELAYS_MS,
    sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    onWaiting = () => {},
    isAlive = () => true,
  } = options;
  for (let attempt = 0; ; attempt += 1) {
    try {
      return { outcome: "valid", user: await check() };
    } catch (error) {
      const kind = classify(error);
      if (kind === "invalid") return { outcome: "invalid" };
      if (kind === "keep" || attempt >= delays.length || !isAlive()) return { outcome: "unverified" };
      onWaiting(attempt + 1);
      await sleep(delays[attempt]);
      if (!isAlive()) return { outcome: "unverified" };
    }
  }
}
