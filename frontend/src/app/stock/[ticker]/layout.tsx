import type { Metadata } from "next";

// The page is a client component, which cannot export metadata; this layout
// carries it, named for the stock.
export async function generateMetadata({ params }: { params: Promise<{ ticker: string }> }): Promise<Metadata> {
  const { ticker } = await params;
  const symbol = decodeURIComponent(ticker).toUpperCase();
  return {
    title: `${symbol}: chart, signal, backtest and options`,
    description: `${symbol}'s price chart, the signal engine's output with its reasons, a five-year backtest against buy-and-hold, and option payoffs.`,
  };
}

export default function Layout({ children }: { children: React.ReactNode }) {
  return children;
}
