import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Emits .next/standalone: the server, plus only the node_modules it actually
  // traced as reachable. The Docker image copies that instead of the whole
  // dependency tree, which is the difference between a ~200MB runtime image and
  // a ~1.2GB one. No effect on `next dev`.
  output: "standalone",
};

export default nextConfig;
