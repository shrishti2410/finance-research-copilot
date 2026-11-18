import type { Metadata } from "next";

// The page is a client component, which cannot export metadata; this layout carries it.
export const metadata: Metadata = {
  title: "Research chat",
  description: "Ask about a company's 10-K filings, prices or signals; every figure in an answer is traced to what a tool returned.",
};

export default function Layout({ children }: { children: React.ReactNode }) {
  return children;
}
