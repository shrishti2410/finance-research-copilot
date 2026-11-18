import type { MetadataRoute } from "next";

import { SITE_URL } from "@/lib/site";

// The public pages only. The app's pages (dashboard, research, stock pages)
// need a session and send a crawler to the sign-in page.
export default function sitemap(): MetadataRoute.Sitemap {
  const pages: { path: string; priority: number }[] = [
    { path: "/", priority: 1 },
    { path: "/about", priority: 0.6 },
    { path: "/login", priority: 0.3 },
  ];
  return pages.map(({ path, priority }) => ({
    url: new URL(path, SITE_URL).toString(),
    changeFrequency: path === "/" ? "daily" : "monthly",
    priority,
  }));
}
