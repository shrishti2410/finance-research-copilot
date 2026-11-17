import type { Metadata } from "next";

// The page is a client component, which cannot export metadata; this layout carries it.
export const metadata: Metadata = {
  title: "Signals dashboard",
  description: "The Nifty 50, the S&P 500 and eight stocks with their day change and the signal engine's output.",
};

export default function Layout({ children }: { children: React.ReactNode }) {
  return children;
}
