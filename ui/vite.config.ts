import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import path from "path";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src")
    }
  },
  server: {
    host: true,
    port: 5173,
    strictPort: true,
    hmr: {
      host: "char.local",
      clientPort: 5173
    },
    origin: "http://192.168.11.180:5173",
    proxy: {
      "/api": "http://127.0.0.1:8787",
      "/events": "http://127.0.0.1:8787"
    }
  }
});
