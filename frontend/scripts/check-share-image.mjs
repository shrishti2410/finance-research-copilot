// Check the link-preview images of a running build, over HTTP only (no browser).
//
//   node scripts/check-share-image.mjs [base URL, default http://localhost:3000]
//
// Reads the landing page's og:image and twitter:image tags, fetches each, and
// checks: HTTP 200, image/png, a PNG of 1200x630, and an alt text that is the
// site name and the landing page's headline (src/lib/site.ts). Exits 1 on any
// failure. tests/test_share_image.py checks the source.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const base = (process.argv[2] || "http://localhost:3000").replace(/\/$/, "");
const site = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "src", "lib", "site.ts"), "utf8");
const headline = site.match(/export const HEADLINE = "([^"]+)"/)[1];
const name = site.match(/export const SITE_NAME = "([^"]+)"/)[1];

const unescape = (s) => s.replace(/&amp;/g, "&").replace(/&#x27;|&#39;/g, "'").replace(/&quot;/g, '"');
const meta = (html, key) => {
  const m = html.match(new RegExp(`<meta[^>]+(?:property|name)="${key}"[^>]+content="([^"]+)"`));
  return m ? unescape(m[1]) : null;
};

const html = await (await fetch(`${base}/`)).text();
let failed = false;
for (const kind of ["og", "twitter"]) {
  const url = meta(html, `${kind}:image`);
  const alt = meta(html, `${kind}:image:alt`);
  if (!url) { console.log(`${kind}: no ${kind}:image tag`); failed = true; continue; }
  const local = base + new URL(url).pathname + new URL(url).search;   // the tag holds the production URL
  const res = await fetch(local);
  const buf = Buffer.from(await res.arrayBuffer());
  const png = buf.subarray(1, 4).toString("ascii") === "PNG";
  const w = png ? buf.readUInt32BE(16) : 0, h = png ? buf.readUInt32BE(20) : 0;
  const ok = res.status === 200 && res.headers.get("content-type") === "image/png" && png && w === 1200 && h === 630
    && alt === `${name}: ${headline}`;
  console.log(`${kind}: ${new URL(url).pathname} status ${res.status} ${res.headers.get("content-type")} ${w}x${h} ${buf.length} bytes; alt ${alt === `${name}: ${headline}` ? "matches the headline" : `MISMATCH: ${alt}`} -> ${ok ? "ok" : "FAIL"}`);
  if (!ok) failed = true;
}
process.exitCode = failed ? 1 : 0;   // not process.exit(): it can abort on Windows with fetch sockets open
