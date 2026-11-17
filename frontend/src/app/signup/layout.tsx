import type { Metadata } from "next";

// The page is a client component, which cannot export metadata; this layout carries it.
export const metadata: Metadata = {
  title: "Create an account",
  description: "Create an account to keep your research conversations.",
};

export default function Layout({ children }: { children: React.ReactNode }) {
  return children;
}
