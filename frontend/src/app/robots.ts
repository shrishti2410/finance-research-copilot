import type { MetadataRoute } from "next";

import { SITE_URL } from "@/lib/site";

// Crawl the public pages; the signed-in app redirects to sign-in, so it is
// left out rather than indexed as a login page several times over.
export default function robots(): MetadataRoute.Robots {
  return {
    rules: {
      userAgent: "*",
      allow: ["/", "/methodology", "/about"],
      disallow: ["/dashboard", "/research", "/stock/", "/backtest", "/chart", "/signup"],
    },
    sitemap: new URL("/sitemap.xml", SITE_URL).toString(),
  };
}
