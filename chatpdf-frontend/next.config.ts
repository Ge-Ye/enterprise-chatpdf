import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  turbopack: { root: __dirname },
  reactStrictMode: true,
};

export default nextConfig;
