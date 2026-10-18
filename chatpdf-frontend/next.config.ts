import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  turbopack: {
    root: __dirname, // 保留原有配置
  },
  // 🚀 新增：让前端代理到 FastAPI 后端
  async rewrites() {
    return [
      {
        source: "/api/:path*", // 前端调用 /api/chat 或 /api/upload
        destination: "http://127.0.0.1:8000/api/:path*", // 转发到后端 FastAPI
      },
    ];
  },
  // 可选：调试时关闭 React 严格模式，避免多次渲染（可保留或删除）
  reactStrictMode: true,
};

export default nextConfig;

