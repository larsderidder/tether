import { defineConfig, loadEnv } from "vite";
import vue from "@vitejs/plugin-vue";
import { VitePWA } from "vite-plugin-pwa";
import path from "path";

export default defineConfig(({ mode }) => {
  // Make dev config portable: allow overriding agent port + HMR/origin via env vars.
  // Vite only exposes VITE_* to the client, but the config itself can read any env.
  const env = loadEnv(mode, process.cwd(), "");

  const agentPort =
    env.TETHER_AGENT_PORT || env.VITE_TETHER_AGENT_PORT || env.VITE_AGENT_PORT || "8787";
  const proxyTarget = `http://127.0.0.1:${agentPort}`;

  const hmrHost = env.VITE_HMR_HOST || undefined;
  const hmrClientPort = env.VITE_HMR_CLIENT_PORT
    ? Number(env.VITE_HMR_CLIENT_PORT)
    : undefined;
  const origin = env.VITE_DEV_ORIGIN || undefined;

  return {
    plugins: [
      vue(),
      VitePWA({
        devOptions: {
          enabled: true,
        },
        registerType: "autoUpdate",
        manifest: {
          name: "Tether",
          short_name: "Tether",
          description: "Control plane for AI coding sessions",
          theme_color: "#0a0a0a",
          background_color: "#0a0a0a",
          display: "standalone",
          icons: [
            {
              src: "/logo.png",
              sizes: "192x192",
              type: "image/png",
            },
            {
              src: "/logo.png",
              sizes: "512x512",
              type: "image/png",
              purpose: "any maskable",
            },
          ],
        },
        workbox: {
          globPatterns: ["**/*.{js,css,html,ico,png,svg,woff,woff2}"],
          navigateFallback: "/index.html",
        },
      }),
    ],
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
    },
    server: {
      host: true,
      port: 5173,
      strictPort: true,
      ...(hmrHost
        ? {
            hmr: {
              host: hmrHost,
              ...(hmrClientPort ? { clientPort: hmrClientPort } : {}),
            },
          }
        : {}),
      ...(origin ? { origin } : {}),
      proxy: {
        "/api": proxyTarget,
        "/events": proxyTarget,
      },
    },
  };
});
