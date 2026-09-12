import type { NextConfig } from "next";
import { PHASE_PRODUCTION_BUILD } from "next/constants";

/**
 * Hosts that mean "this machine", whichever machine happens to be running the
 * code. That ambiguity is the whole problem: in the browser it resolves to the
 * *visitor's* machine, not the server's.
 */
const LOOPBACK =
  /^(?:localhost|127(?:\.\d{1,3}){3}|\[::1\]|::1|0\.0\.0\.0)$/i;

/** Service names from docker-compose.yml: reachable only on the Docker network. */
const COMPOSE_SERVICE_NAMES = new Set([
  "backend",
  "frontend",
  "postgres",
  "redis",
  "ollama",
]);

/**
 * Refuse to produce a build whose client cannot reach its API.
 *
 * NEXT_PUBLIC_* values are substituted into the client bundle as literals at
 * build time, so this is decided here and cannot be corrected later by an
 * environment variable on the server. A wrong value produces the worst kind of
 * failure: the app builds, deploys, renders, routes, and then sends every
 * request to the visitor's own localhost. Nothing appears in the server logs,
 * because nothing ever reaches the server.
 *
 * Hence a thrown error rather than a warning. A warning scrolls past in build
 * output nobody reads; this is the one setting where being unable to ship beats
 * shipping something broken.
 *
 * Deliberately *not* checked during `next dev`, where localhost is correct.
 *
 * The escape hatch is for building a container to run on this machine:
 *
 *     ALLOW_LOCALHOST_API_BASE=1 npm run build
 *     docker compose build --build-arg ALLOW_LOCALHOST_API_BASE=1
 *
 * It has to be typed out, which is the point -- pointing a build at localhost
 * should be a deliberate act, not what happens when nobody sets anything.
 */
function assertApiBaseIsReachable(): void {
  const value = (process.env.NEXT_PUBLIC_API_BASE ?? "").trim();
  const allowLocalhost = process.env.ALLOW_LOCALHOST_API_BASE === "1";

  const explain = (problem: string) =>
    new Error(
      `\n\nNEXT_PUBLIC_API_BASE ${problem}\n\n` +
        `  This value is compiled into the browser bundle, so it must be a URL\n` +
        `  the visitor's browser can reach -- not a compose service name, and\n` +
        `  not this machine.\n\n` +
        `  Set it to the API's public address:\n` +
        `    docker compose build       (reads PUBLIC_API_BASE from .env)\n` +
        `    docker build --build-arg NEXT_PUBLIC_API_BASE=https://api.example.com\n` +
        `    NEXT_PUBLIC_API_BASE=https://api.example.com npm run build\n\n` +
        `  Building a container to run on this machine? Say so explicitly:\n` +
        `    ALLOW_LOCALHOST_API_BASE=1 ...\n\n` +
        `  See docs/DEPLOYMENT.md section 6.\n`,
    );

  if (!value) {
    throw explain("is not set.");
  }

  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw explain(`is not a valid URL: ${JSON.stringify(value)}`);
  }

  // `new URL("backend:8000")` does not throw: Node reads it as protocol
  // "backend:" with an empty hostname. So parsing successfully proves nothing,
  // and without this the exact mistake the message below warns about -- a bare
  // compose service name -- built cleanly.
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw explain(
      `must be an http(s) URL, but its protocol is "${url.protocol}": ` +
        `${JSON.stringify(value)}`,
    );
  }

  if (!url.hostname) {
    throw explain(`has no host: ${JSON.stringify(value)}`);
  }

  if (LOOPBACK.test(url.hostname) && !allowLocalhost) {
    throw explain(
      `points at ${url.hostname}, which in a browser means the visitor's own ` +
        `machine.`,
    );
  }

  // This stack's own compose service names. They resolve on the Docker network
  // and nowhere else -- certainly not in a browser -- so one here is always the
  // mistake, never an intranet host somebody meant. Named explicitly rather
  // than rejecting all dotless hostnames, which would fail a real intranet
  // deployment.
  if (COMPOSE_SERVICE_NAMES.has(url.hostname.toLowerCase())) {
    throw explain(
      `points at "${url.hostname}", a compose service name. That resolves on ` +
        `the Docker network, which the browser is not on.`,
    );
  }
}

const config = (phase: string): NextConfig => {
  if (phase === PHASE_PRODUCTION_BUILD) {
    assertApiBaseIsReachable();
  }

  return {
    // Emits .next/standalone: the server, plus only the node_modules it
    // actually traced as reachable. The Docker image copies that instead of the
    // whole dependency tree, which is the difference between a ~200MB runtime
    // image and a ~1.2GB one. No effect on `next dev`.
    output: "standalone",
  };
};

export default config;
