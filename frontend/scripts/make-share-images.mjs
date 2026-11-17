// Make the link-preview images from the site's own mark and dashboard screenshot.
//
//   node scripts/make-share-images.mjs [--edge <path to Edge or Chrome>]
//
// Writes, for Next.js's metadata file conventions:
//   src/app/opengraph-image.png  1200x630, with opengraph-image.alt.txt
//   src/app/twitter-image.png    the same image, with twitter-image.alt.txt
//   src/app/apple-icon.png       180x180, from src/app/icon.svg
// No network: the card is plain HTML rendered in headless Edge, using the
// committed screenshot public/shots/hero-dashboard.webp. The browser is closed
// on every exit path, killed with its process tree if close fails.

import { spawnSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-core";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const i = args.indexOf("--edge");
const EDGE = i >= 0 ? args[i + 1] : "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";

const ALT = "Finance Research Copilot: research companies and test trading strategies, with every number checked. Shows the signals dashboard.";

const shot = readFileSync(join(ROOT, "public", "shots", "hero-dashboard.webp")).toString("base64");
const icon = readFileSync(join(ROOT, "src", "app", "icon.svg"), "utf8");

const CARD = `<!doctype html><html><head><meta charset="utf-8"><style>
  * { box-sizing: border-box; margin: 0; }
  body { width: 1200px; height: 630px; background: #0b0f14; color: #e8edf3; overflow: hidden;
         font-family: "Segoe UI", system-ui, sans-serif; position: relative; }
  .glow { position: absolute; inset: 0; background: radial-gradient(900px 500px at 85% 20%, rgba(59,142,234,.18), transparent 60%); }
  .left { position: absolute; left: 64px; top: 72px; width: 520px; }
  .brand { display: flex; align-items: center; gap: 14px; font-size: 26px; font-weight: 600; }
  .brand svg { width: 44px; height: 44px; }
  h1 { margin-top: 44px; font-size: 54px; line-height: 1.08; letter-spacing: -1px; font-weight: 650; }
  p { margin-top: 26px; font-size: 24px; line-height: 1.4; color: #a3afbf; }
  .tags { position: absolute; left: 64px; bottom: 56px; font-size: 20px; color: #7d899b; }
  .frame { position: absolute; left: 640px; top: 96px; width: 640px; border: 1px solid #253041; border-radius: 16px;
           overflow: hidden; background: #11161d; box-shadow: 0 30px 80px rgba(0,0,0,.5); }
  .bar { height: 30px; background: #171e28; border-bottom: 1px solid #253041; display: flex; gap: 7px; align-items: center; padding-left: 14px; }
  .bar i { width: 10px; height: 10px; border-radius: 50%; background: #253041; display: block; }
  .frame img { display: block; width: 100%; }
</style></head><body><div class="glow"></div>
  <div class="left">
    <div class="brand">${icon}<span>Finance Research Copilot</span></div>
    <h1>Research companies and test trading strategies</h1>
    <p>Answers from SEC filings and backtests computed in code, with every number checked.</p>
  </div>
  <div class="tags">SEC 10-K filings · NSE and US stocks · honest backtests</div>
  <div class="frame"><div class="bar"><i></i><i></i><i></i></div>
    <img src="data:image/webp;base64,${shot}" alt=""></div>
</body></html>`;

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

const browser = await puppeteer.launch({ executablePath: EDGE, headless: true });
const proc = browser.process();
process.on("exit", () => killTree(proc));
try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1200, height: 630, deviceScaleFactor: 1 });
  await page.setContent(CARD, { waitUntil: "load" });
  const og = await page.screenshot({ type: "png" });
  for (const name of ["opengraph-image", "twitter-image"]) {
    writeFileSync(join(ROOT, "src", "app", `${name}.png`), og);
    writeFileSync(join(ROOT, "src", "app", `${name}.alt.txt`), ALT);
  }
  await page.setViewport({ width: 180, height: 180, deviceScaleFactor: 1 });
  await page.setContent(`<html><body style="margin:0;background:#0b0f14">${icon.replace("<svg ", '<svg width="180" height="180" ')}</body></html>`);
  writeFileSync(join(ROOT, "src", "app", "apple-icon.png"), await page.screenshot({ type: "png" }));
  console.log(`opengraph-image.png and twitter-image.png ${(og.length / 1024).toFixed(0)} KB; apple-icon.png written`);
} finally {
  try {
    await Promise.race([browser.close(), new Promise((_, no) => setTimeout(() => no(new Error("timeout")), 5000))]);
  } catch {
    /* fall through */
  }
  killTree(proc);
}
