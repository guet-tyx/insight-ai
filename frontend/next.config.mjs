/** @type {import('next').NextConfig} */
// W11：standalone 输出 —— Docker 镜像运行阶段仅携带最小依赖（配合 multi-stage）
const nextConfig = {
  output: "standalone",
};

export default nextConfig;
