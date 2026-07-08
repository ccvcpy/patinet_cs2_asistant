import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

const apiProxy = {
  "/api": "http://127.0.0.1:8765",
};

export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: apiProxy,
  },
  preview: {
    proxy: apiProxy,
  },
});
