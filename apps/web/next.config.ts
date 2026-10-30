import type { NextConfig } from "next";
const config: NextConfig = {
  distDir: process.env.LEAD_WEB_DIST_DIR || ".next",
  experimental: { proxyClientMaxBodySize: "129mb", proxyTimeout: 120000 },
  async rewrites() {
    return ["api/:path*", "r/:path*", "collect/:path*", "lead.js"].map(
      (path) => ({
        source: "/" + path,
        destination: "http://127.0.0.1:8088/" + path,
      }),
    );
  },
  devIndicators: false,
};
export default config;
