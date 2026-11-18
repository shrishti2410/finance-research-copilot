// Site-wide constants, in a module with no "use client" so server components
// get the values themselves, not client references.
export const GITHUB_URL = "https://github.com/shrishti2410/finance-research-copilot";

export const SITE_NAME = "Finance Research Copilot";
export const SITE_DESCRIPTION =
  "Research public companies from their SEC filings, with a strategy audit of classic trading rules alongside, and every figure computed in code and traced to its source.";
// The public address, for absolute URLs in sharing tags and the sitemap.
// NEXT_PUBLIC_SITE_URL overrides it (a preview deploy, a custom domain).
export const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL ?? "https://finance-research-copilot-swart.vercel.app";

// The author, for /about and the "Built by" section. The LinkedIn and resume
// links are left empty until the author supplies them: an empty one is shown
// as "coming soon", never as an invented address.
export const AUTHOR = {
  name: "Shrishti Chandak",
  github: "https://github.com/shrishti2410",
  linkedin: "",
  resume: "",
};
