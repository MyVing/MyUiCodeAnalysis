import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 端口避开前端工作台 dev server（5173）；strictPort 保证「启动失败」是显式的
// 而不是静默换端口，让采集测试的环境问题可诊断。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5199,
    strictPort: true,
  },
});
