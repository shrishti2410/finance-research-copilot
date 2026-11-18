import type { MetadataRoute } from "next";

import { SITE_URL } from "@/lib/site";

// Everything may be crawled except the sign-up form. The app's pages send a
// visitor without a session to sign-in, which is harmless to crawl; blocking
// them in robots.txt made Lighthouse report /dashboard as not crawlable (SEO
// 63). The sitemap lists the public pages, which are the ones worth indexing.
export default function robots(): MetadataRoute.Robots {
  return {
    rules: { userAgent: "*", allow: "/", disallow: ["/signup"] },
    sitemap: new URL("/sitemap.xml", SITE_URL).toString(),
  };
}
