import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";

import { SiteFooter, SiteHeader } from "@/components/SiteChrome";
import { AuthProvider } from "@/lib/auth";
import "./globals.css";

// Self-hosted by next/font at build time: no request to Google from a visitor.
const sans = Geist({ subsets: ["latin"], variable: "--font-geist-sans" });
const mono = Geist_Mono({ subsets: ["latin"], variable: "--font-geist-mono" });

export const metadata: Metadata = {
  title: "Finance Research Copilot",
  description:
    "Answers about public companies grounded in their SEC filings, and a trading-signal lab that reports honestly when it has no edge.",
};

export const viewport: Viewport = { themeColor: "#0b0f14" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${sans.variable} ${mono.variable}`}>
      <body className="flex min-h-screen flex-col bg-bg text-fg antialiased">
        <AuthProvider>
          <SiteHeader />
          <div className="flex-1">{children}</div>
          <SiteFooter />
        </AuthProvider>
      </body>
    </html>
  );
}
