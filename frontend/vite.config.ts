import { defineConfig, loadEnv } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const apiProxy = {
    "/api": env.VITE_API_TARGET || "http://127.0.0.1:8765",
  };

  return {
    plugins: [vue()],
    server: {
      proxy: apiProxy,
    },
    preview: {
      proxy: apiProxy,
    },
  };
});
