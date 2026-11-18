import { ImageResponse } from "next/og";

import { ENTRY_POINTS, HEADLINE, SITE_NAME } from "@/lib/site";

// The link-preview card, drawn in code (next/og) rather than from a
// screenshot, so it never shows a stale page and needs no browser to make.
// The headline is the landing page's own (HEADLINE in src/lib/site.ts).
// tests/test_share_image.py checks this file; scripts/check-share-image.mjs
// checks the built route.

export const alt = `${SITE_NAME}: ${HEADLINE}`;
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

const BG = "#0b0f14";
const FG = "#e6edf3";
const MUTED = "#9aa7b4";
const ACCENT = "#3b82f6";
const BORDER = "#263241";

export default function Image() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          padding: "64px 72px",
          background: BG,
          color: FG,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
          <div
            style={{
              width: 52,
              height: 52,
              borderRadius: 12,
              background: ACCENT,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#ffffff" strokeWidth="2.5">
              <path d="M3 3v18h18" />
              <path d="M7 15l4-4 3 3 5-6" />
            </svg>
          </div>
          <div style={{ fontSize: 32, fontWeight: 600 }}>{SITE_NAME}</div>
        </div>

        <div style={{ display: "flex", fontSize: 62, fontWeight: 700, lineHeight: 1.12, letterSpacing: -1 }}>
          {HEADLINE}
        </div>

        <div style={{ display: "flex", gap: 20 }}>
          {ENTRY_POINTS.map((label) => (
            <div
              key={label}
              style={{
                display: "flex",
                padding: "14px 24px",
                borderRadius: 14,
                border: `2px solid ${BORDER}`,
                color: MUTED,
                fontSize: 28,
              }}
            >
              {label}
            </div>
          ))}
        </div>
      </div>
    ),
    size,
  );
}
