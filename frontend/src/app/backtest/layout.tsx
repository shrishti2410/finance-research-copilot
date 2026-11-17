import type { Metadata } from "next";

// The page is a client component, which cannot export metadata; this layout carries it.
export const metadata: Metadata = {
  title: "Strategy backtester",
  description: "Backtest a textbook trading rule on a stock, after trading costs, against buy-and-hold.",
};

export default function Layout({ children }: { children: React.ReactNode }) {
  return children;
}
