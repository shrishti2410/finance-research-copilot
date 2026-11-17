// Regenerate the landing page's screenshots from a running production build.
//
//   npm run build && npm start            # the front end, built for the API below
//   node scripts/capture-screenshots.mjs --web http://localhost:3000 --api http://127.0.0.1:8000
//
// Options:
//   --web <url>      the front end (a production build: `next start`, not `next dev`)
//   --api <url>      the API that front end was built for
//   --signup         sign up a throwaway account instead of using /auth/demo
//                    (for a local API without the demo account; never against production)
//   --edge <path>    the Edge or Chrome executable (default: Edge's Windows path)
//
// Writes compressed WebP files to public/shots/ and their sizes, alt text,
// page paths and capture date to src/content/screenshots.json, which the
// landing page reads. No shot asks the model anything.
//
// The browser is closed on every exit path: normally with browser.close(), and
// if that fails or the run errors, times out or is interrupted, by killing its
// whole process tree.

import { spawnSync } from "node:child_process";
import { mkdirSync, statSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-core";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const OUT_DIR = join(ROOT, "public", "shots");
const MANIFEST = join(ROOT, "src", "content", "screenshots.json");

const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : fallback;
};
const WEB = opt("web", "http://localhost:3000");
const API = opt("api", "http://127.0.0.1:8000");
const EDGE = opt("edge", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe");
const SIGNUP = args.includes("--signup");
const HARD_LIMIT_MS = 20 * 60 * 1000;

const NSE = "SBIN.NS";

// name -> what to capture. `scale` is the device pixel ratio: 1 for the large
// shots, 0.5 for the small feature-card copies (half the pixels, a quarter of
// the bytes). `clip` "panel" captures the active tab panel; "viewport" the
// visible window.
const SHOTS = [
  { name: "hero-dashboard", path: "/dashboard", wait: "[aria-label$='open the stock page']", clip: "viewport",
    width: 1280, height: 800, scale: 1, alt: "The watchlist dashboard: the Nifty 50 and S&P 500, and a card per stock with its price, day change, 60-day sparkline and signal." },
  { name: "tour-chart", path: `/stock/${NSE}?tab=chart`, wait: '[data-loaded="chart"]', clip: "panel",
    width: 1280, scale: 1, alt: "State Bank of India's Chart tab: candles, 50- and 200-day averages, volume, RSI and MACD, with arrows where the signal went in or out." },
  { name: "tour-signal", path: `/stock/${NSE}?tab=signal`, wait: "[role=tabpanel]", clip: "panel",
    width: 1280, scale: 1, alt: "State Bank of India's Signal tab: the vote of each rule with its weight, and the market regime." },
  { name: "tour-backtest", path: `/stock/${NSE}?tab=backtest`, wait: "[role=tabpanel]", clip: "panel",
    width: 1280, scale: 1, alt: "State Bank of India's Backtest tab: the walk-forward result after costs, against buy-and-hold and random timing." },
  { name: "tour-options", path: `/stock/${NSE}?tab=options`, wait: "[role=tabpanel] dl", clip: "panel",
    width: 1280, scale: 1, alt: "State Bank of India's Options tab: an option structure with estimated premium, payoff at expiry, breakevens and Greeks." },
  { name: "tour-research", path: "/stock/NVDA?tab=research", wait: '[data-loaded="research"]', clip: "panel",
    width: 1280, scale: 1, alt: "NVIDIA's Research tab: its 10-K filings are indexed, and suggested questions open the research chat." },
];
// The feature cards reuse four of these pages at half the pixel density.
const FEATURES = { "feature-research": "tour-research", "feature-signal": "tour-signal",
                   "feature-backtest": "tour-backtest", "feature-options": "tour-options" };

function killTree(proc) {
  try {
    if (proc && proc.exitCode === null && proc.pid) {
      if (process.platform === "win32") spawnSync("taskkill", ["/PID", String(proc.pid), "/T", "/F"], { stdio: "ignore" });
      else proc.kill("SIGKILL");
    }
  } catch {
    /* already gone */
  }
}

async function closeBrowser(browser) {
  const proc = browser.process();
  try {
    await Promise.race([browser.close(), new Promise((_, no) => setTimeout(() => no(new Error("timeout")), 5000))]);
  } catch {
    /* fall through */
  }
  killTree(proc);
}

async function token() {
  const r = SIGNUP
    ? await fetch(`${API}/auth/signup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: `screenshots-${Date.now()}@example.com`, password: "screenshots-only-account" }),
      })
    : await fetch(`${API}/auth/demo`, { method: "POST" });
  if (!r.ok) throw new Error(`sign-in for screenshots: HTTP ${r.status}`);
  return (await r.json()).access_token;
}


// One page load per shot; each scale in `scales` is captured from it.
async function capture(browser, access, shot, scales) {
  const page = await browser.newPage();
  try {
    await page.setViewport({ width: shot.width, height: shot.height ?? 900, deviceScaleFactor: scales[0] });
    await page.goto(`${WEB}/login`, { waitUntil: "domcontentloaded" });
    await page.evaluate((t) => localStorage.setItem("frc.token", t), access);
    await page.goto(`${WEB}${shot.path}`, { waitUntil: "networkidle0", timeout: 180_000 });
    await page.waitForSelector(shot.wait, { timeout: 180_000 });
    await new Promise((r) => setTimeout(r, 1500)); // charts finish drawing
    if (await page.$("nextjs-portal")) throw new Error("this is a dev server; capture from a production build");

    const out = {};
    for (const scale of scales) {
      if (scale !== scales[0]) {
        await page.setViewport({ width: shot.width, height: shot.height ?? 900, deviceScaleFactor: scale });
        await new Promise((r) => setTimeout(r, 1500));
      }
      let clip;
      if (shot.clip === "panel") {
        const sel = '[role=tabpanel][data-state="active"]';
        // A 16px margin round the panel, so its cards' borders are not cut.
        clip = await page.$eval(sel, (el, pad) => {
          const r = el.getBoundingClientRect();
          const x = Math.max(0, r.x + window.scrollX - pad);
          return { x, y: Math.max(0, r.y + window.scrollY - pad), width: Math.min(r.width + 2 * pad, window.innerWidth - x),
                   height: r.height + 2 * pad };
        }, 16);
        clip.height = Math.min(clip.height, 820);
      }
      const buf = await page.screenshot({ type: "webp", quality: 72, ...(clip ? { clip, captureBeyondViewport: true } : {}) });
      const css = clip ?? { width: shot.width, height: shot.height ?? 900 };
      out[scale] = { buf, width: Math.round(css.width * scale), height: Math.round(css.height * scale) };
    }
    return out;
  } finally {
    await page.close();
  }
}

async function main() {
  mkdirSync(OUT_DIR, { recursive: true });
  const access = await token();
  const browser = await puppeteer.launch({ executablePath: EDGE, headless: true });
  const proc = browser.process();
  process.on("exit", () => killTree(proc));
  for (const sig of ["SIGINT", "SIGTERM"]) process.on(sig, async () => { await closeBrowser(browser); process.exit(130); });
  const limit = setTimeout(async () => {
    console.error("capture exceeded its time limit; closing the browser");
    await closeBrowser(browser);
    process.exit(1);
  }, HARD_LIMIT_MS);
  limit.unref();

  const images = {};
  try {
    const record = (name, shot, out) => {
      const file = join(OUT_DIR, `${name}.webp`);
      writeFileSync(file, out.buf);
      images[name] = { src: `/shots/${name}.webp`, width: out.width, height: out.height, alt: shot.alt,
                       path: shot.path.split("?")[0] };
      console.log(`${name}.webp  ${out.width}x${out.height}  ${(statSync(file).size / 1024).toFixed(0)} KB`);
    };
    for (const shot of SHOTS) {
      const feature = Object.keys(FEATURES).find((k) => FEATURES[k] === shot.name);
      const outs = await capture(browser, access, shot, feature ? [shot.scale, 0.5] : [shot.scale]);
      record(shot.name, shot, outs[shot.scale]);
      if (feature) record(feature, shot, outs[0.5]);
    }
  } finally {
    await closeBrowser(browser);
  }
  const manifest = {
    _comment: "Written by scripts/capture-screenshots.mjs; do not edit by hand. Regenerate after a UI change.",
    captured: new Date().toISOString().slice(0, 10),
    images,
  };
  writeFileSync(MANIFEST, `${JSON.stringify(manifest, null, 2)}\n`);
  console.log(`wrote ${MANIFEST}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
