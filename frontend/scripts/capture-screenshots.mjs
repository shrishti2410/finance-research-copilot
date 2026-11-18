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
//   --ask <question> also capture the Research shots from a real answer to this one
//                    question. It costs one model run on whatever provider the API uses.
//                    Without it, the Research shots show NVIDIA's Research tab.
//   --only <names>   comma-separated output names to capture; the others keep their
//                    files and manifest entries
//   --edge <path>    the Edge or Chrome executable (default: Edge's Windows path)
//
// Writes compressed WebP files to public/shots/ and their sizes, alt text and
// page paths to src/content/screenshots.json, which the landing page reads.
//
// The browser is closed on every exit path: normally with browser.close(), and
// if that fails or the run errors, times out or is interrupted, by killing its
// whole process tree.

import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
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
const ASK = opt("ask", null);
const ONLY = opt("only", null)?.split(",");
const HARD_LIMIT_MS = 25 * 60 * 1000;

const NSE = "SBIN.NS";
// Large shots: the tour and the hero. Feature crops: a narrow page at twice the
// pixel density, cut to one element, so the text stays readable on a card.
const WIDE = { width: 1280, height: 900, scale: 1 };
const NARROW = { width: 700, height: 900, scale: 2 };

// Each page is loaded once; each of its outputs is cut from it. `clip`:
//   "viewport"  the visible window
//   "panel"     the active tab panel, with a margin
//   a function  run in the page, returning the element(s) to cut round
const PAGES = [
  {
    path: "/dashboard", wait: "[data-stock-card]", view: { ...WIDE, scale: 1.5 }, zoom: 0.78,
    outputs: [{ name: "hero-dashboard", clip: "viewport",
      alt: "The watchlist dashboard: the Nifty 50 with five NSE stocks, and the S&P 500 with three US stocks, each with its price, day change, 60-day sparkline and signal." }],
  },
  {
    path: `/stock/${NSE}?tab=chart`, wait: '[data-loaded="chart"]', view: WIDE,
    outputs: [{ name: "tour-chart", clip: "panel",
      alt: "State Bank of India's Chart tab: candles, 50- and 200-day averages, volume, RSI and MACD, with arrows where the signal went in or out." }],
  },
  {
    path: `/stock/${NSE}?tab=signal`, wait: '[data-loaded="signal"]', view: WIDE,
    outputs: [{ name: "tour-signal", clip: "panel",
      alt: "State Bank of India's Signal tab: the backtest verdict, the engine's output, the market regime and the vote of each rule." }],
  },
  {
    path: `/stock/${NSE}?tab=signal`, wait: '[data-loaded="signal"]', view: NARROW,
    // The verdict and the first two votes; the three summary cards between
    // them are hidden for this shot.
    prepare: () => {
      const root = document.querySelector('[data-loaded="signal"]');
      for (const el of root.children) if (el.matches("div.grid, ul")) el.style.display = "none";
    },
    outputs: [{ name: "feature-signal",
      clip: () => {
        const root = document.querySelector('[data-loaded="signal"]');
        return [root.children[0], ...root.querySelectorAll("#reasons + ol > li:nth-child(-n+2)")];
      },
      alt: "The signal's verdict, no edge in the backtest, and the first two rule votes with their weights and reasons." }],
  },
  {
    path: `/stock/${NSE}?tab=backtest`, wait: '[data-loaded="backtest"]', view: WIDE,
    outputs: [{ name: "tour-backtest", clip: "panel",
      alt: "State Bank of India's Backtest tab: the walk-forward result after costs, against buy-and-hold and random timing." }],
  },
  {
    path: `/stock/${NSE}?tab=backtest`, wait: '[data-loaded="backtest"] figure', view: NARROW,
    outputs: [{ name: "feature-backtest",
      clip: () => [document.querySelector('[data-loaded="backtest"] figure').closest(".p-5")],
      alt: "The signal's equity curve against buy-and-hold, over five years." }],
  },
  {
    path: `/stock/${NSE}?tab=options`, wait: "[role=tabpanel] dl", view: WIDE,
    outputs: [{ name: "tour-options", clip: "panel",
      alt: "State Bank of India's Options tab: an option structure with estimated premium, payoff at expiry, breakevens and Greeks." }],
  },
  {
    path: `/stock/${NSE}?tab=options`, wait: "[role=tabpanel] dl", view: NARROW,
    outputs: [{ name: "feature-options",
      clip: () => {
        const dl = document.querySelector("[role=tabpanel] dl");
        const card = dl.closest(".p-5") ?? dl.parentElement;
        return [card.querySelector("h2, h3") ?? card, dl];
      },
      alt: "The payoff at expiry of a bull call spread, with its maximum loss, maximum profit and breakeven." }],
  },
  ASK
    ? {
        path: `/research?q=${encodeURIComponent(ASK)}`, wait: "answer", view: { ...WIDE, height: 1000 },
        outputs: [
          { name: "tour-research", clip: () => [...document.querySelectorAll("main ul.space-y-6 > li")],
            alt: `The research chat answering "${ASK}" from the 10-K, with the tools it used as sources.` },
          { name: "feature-research", view: NARROW, clip: () => [...document.querySelectorAll("main ul.space-y-6 > li")],
            alt: `An answer to "${ASK}", with its sources from the 10-K.` },
        ],
      }
    : {
        path: "/stock/NVDA?tab=research", wait: '[data-loaded="research"]', view: WIDE,
        outputs: [
          { name: "tour-research", clip: "panel",
            alt: "NVIDIA's Research tab: its 10-K filings are indexed, and suggested questions open the research chat." },
          { name: "feature-research", view: NARROW, clip: "panel",
            alt: "NVIDIA's Research tab: its 10-K filings are indexed, and suggested questions open the research chat." },
        ],
      },
];

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

// The question in the URL is asked once the page has checked the session. Done
// when an answer follows the question and the composer is free again. A plain
// polling loop: the page rewrites its URL while it works, which can reset
// puppeteer's own wait functions.
async function waitForAnswer(page) {
  const deadline = Date.now() + 600_000;
  while (Date.now() < deadline) {
    const state = await page
      .evaluate(() => ({
        items: document.querySelectorAll("main ul.space-y-6 > li").length,
        free: Boolean(document.querySelector('textarea[placeholder="Ask a question…"]')),
        thinking: document.body.innerText.includes("Thinking…"),
      }))
      .catch(() => null);
    if (state && state.items >= 2 && state.free && !state.thinking) {
      // Open the sources under the answer.
      await page.evaluate(() => {
        const last = [...document.querySelectorAll("main ul.space-y-6 > li")].at(-1);
        last?.querySelector(".border-t button")?.click();
      });
      await new Promise((r) => setTimeout(r, 800));
      return;
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
  const debug = join(ROOT, "capture-debug.png");
  await page.screenshot({ path: debug });
  throw new Error(`the research answer never appeared in 10 minutes; the page as it was: ${debug}`);
}

async function rectOf(page, clip, pad) {
  return page.evaluate(
    (src, pad) => {
      let els;
      if (src === "panel") els = [document.querySelector('[role=tabpanel][data-state="active"]')];
      else els = new Function(`return (${src})()`)();
      els = els.filter(Boolean);
      if (!els.length) throw new Error("nothing to capture");
      const rects = els.map((el) => el.getBoundingClientRect());
      const left = Math.min(...rects.map((r) => r.left)) - pad;
      const top = Math.min(...rects.map((r) => r.top)) - pad;
      const right = Math.max(...rects.map((r) => r.right)) + pad;
      const bottom = Math.max(...rects.map((r) => r.bottom)) + pad;
      const x = Math.max(0, left + window.scrollX);
      return { x, y: Math.max(0, top + window.scrollY), width: Math.min(right - left, document.documentElement.clientWidth - x),
               height: bottom - top };
    },
    clip === "panel" ? "panel" : clip.toString(),
    pad,
  );
}

async function capturePage(browser, access, spec) {
  const page = await browser.newPage();
  const out = [];
  try {
    const setView = (v) => page.setViewport({ width: v.width, height: v.height, deviceScaleFactor: v.scale });
    await setView(spec.view);
    await page.goto(`${WEB}/login`, { waitUntil: "domcontentloaded" });
    await page.evaluate((t) => localStorage.setItem("frc.token", t), access);
    // The research page streams its answer over an open connection, so the
    // network never goes idle there.
    await page.goto(`${WEB}${spec.path}`, { waitUntil: spec.wait === "answer" ? "domcontentloaded" : "networkidle0", timeout: 180_000 });
    if (spec.wait === "answer") await waitForAnswer(page);
    else await page.waitForSelector(spec.wait, { timeout: 180_000 });
    if (await page.$("nextjs-portal")) throw new Error("this is a dev server; capture from a production build");
    if (spec.zoom) await page.evaluate((z) => (document.documentElement.style.zoom = String(z)), spec.zoom);
    if (spec.prepare) await page.evaluate(spec.prepare);
    await new Promise((r) => setTimeout(r, 1500)); // charts finish drawing

    for (const o of spec.outputs) {
      if (ONLY && !ONLY.includes(o.name)) continue;
      const view = o.view ?? spec.view;
      if (view !== spec.view) {
        await setView(view);
        await new Promise((r) => setTimeout(r, 1500));
      }
      const clip = o.clip === "viewport" ? null : await rectOf(page, o.clip, o.clip === "panel" ? 16 : 12);
      if (clip) clip.height = Math.min(clip.height, o.clip === "panel" ? 820 : 900);
      const buf = await page.screenshot({ type: "webp", quality: 72, ...(clip ? { clip, captureBeyondViewport: true } : {}) });
      const css = clip ?? { width: view.width, height: view.height };
      out.push({ output: o, buf, width: Math.round(css.width * view.scale), height: Math.round(css.height * view.scale),
                 path: spec.path.split("?")[0] });
    }
  } finally {
    await page.close();
  }
  return out;
}

async function main() {
  mkdirSync(OUT_DIR, { recursive: true });
  const previous = existsSync(MANIFEST) ? JSON.parse(readFileSync(MANIFEST, "utf8")).images ?? {} : {};
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

  const images = { ...previous };
  try {
    for (const spec of PAGES) {
      if (ONLY && !spec.outputs.some((o) => ONLY.includes(o.name))) continue;
      for (const shot of await capturePage(browser, access, spec)) {
        const name = shot.output.name;
        const file = join(OUT_DIR, `${name}.webp`);
        writeFileSync(file, shot.buf);
        images[name] = { src: `/shots/${name}.webp`, width: shot.width, height: shot.height, alt: shot.output.alt, path: shot.path };
        console.log(`${name}.webp  ${shot.width}x${shot.height}  ${(statSync(file).size / 1024).toFixed(0)} KB`);
      }
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
