import type { Metadata } from "next";

// The page is a client component, which cannot export metadata; this layout carries it.
export const metadata: Metadata = {
  title: "Price chart",
  description: "Daily candles with moving averages, Bollinger bands, RSI and MACD.",
};

export default function Layout({ children }: { children: React.ReactNode }) {
  return children;
}
