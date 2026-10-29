import type { NextConfig } from "next";
const config: NextConfig = {
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
