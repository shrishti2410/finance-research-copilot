import type { Metadata } from "next";

// The page is a client component, which cannot export metadata; this layout carries it.
export const metadata: Metadata = {
  title: "Sign in",
  description: "Sign in, or try the demo without an account.",
};

export default function Layout({ children }: { children: React.ReactNode }) {
  return children;
}
