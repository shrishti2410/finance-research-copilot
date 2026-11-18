// Make the Apple touch icon from the site's mark.
//
//   node scripts/make-share-images.mjs [--edge <path to Edge or Chrome>]
//
// Writes, for Next.js's metadata file conventions:
//   src/app/apple-icon.png       180x180, from src/app/icon.svg
// The link-preview images are no longer made here: since 2026-11-18 they are
// drawn in code (src/app/opengraph-image.tsx, twitter-image.tsx). This script
// runs a browser, so it is run only when the user asks (CLAUDE.md rule 6).
// No network: the icon is rendered in headless Edge. The browser is closed
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

const icon = readFileSync(join(ROOT, "src", "app", "icon.svg"), "utf8");

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
  await page.setViewport({ width: 180, height: 180, deviceScaleFactor: 1 });
  await page.setContent(`<html><body style="margin:0;background:#0b0f14">${icon.replace("<svg ", '<svg width="180" height="180" ')}</body></html>`);
  writeFileSync(join(ROOT, "src", "app", "apple-icon.png"), await page.screenshot({ type: "png" }));
  console.log("apple-icon.png written");
} finally {
  try {
    await Promise.race([browser.close(), new Promise((_, no) => setTimeout(() => no(new Error("timeout")), 5000))]);
  } catch {
    /* fall through */
  }
  killTree(proc);
}
