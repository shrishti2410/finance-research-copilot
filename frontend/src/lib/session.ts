/**
 * One place for "the token is no longer good", so every call site handles it
 * the same way.
 *
 * The API layer cannot navigate and must not import React; the auth provider
 * can do both but does not make the requests. This is the seam between them: a
 * single registered handler, invoked on any 401.
 *
 * Without it, an expired token surfaced as a small red "Not authenticated."
 * under a chat page still showing the previous session's conversations, and the
 * only way out was clearing localStorage by hand. A token lasts 30 minutes, so
 * that is not an edge case -- it is what happens to any tab left open over
 * lunch.
 */

type Handler = () => void;

let handler: Handler | null = null;

/** Called by the auth provider on mount. Returns an unsubscribe. */
export function onSessionExpired(next: Handler): () => void {
  handler = next;
  return () => {
    if (handler === next) handler = null;
  };
}

/** Called by the API layer whenever the server rejects our credentials. */
export function sessionExpired(): void {
  handler?.();
}
