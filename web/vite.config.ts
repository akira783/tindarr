import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const webRoot = fileURLToPath(new URL(".", import.meta.url));
const sharedI18n = fileURLToPath(new URL("../shared/i18n", import.meta.url));

// The console is served by the Tindarr server under a strict CSP (docs/adr/0009):
// no inline script, no inline style, no data: URI, everything under /assets/.
// `scripts/check-csp.mjs` verifies the built output after every `npm run build`.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@i18n": sharedI18n },
  },
  server: {
    // Same-origin development: the dev server proxies /api to a local Tindarr
    // server without rewriting Host or Origin, so cookies and the CSRF Origin
    // check behave as in production (docs/architecture.md, "Development").
    fs: { allow: [webRoot, sharedI18n] },
    proxy: {
      "/api": {
        target: process.env["TINDARR_DEV_SERVER"] ?? "http://localhost:8787",
        changeOrigin: false,
      },
      "/healthz": {
        target: process.env["TINDARR_DEV_SERVER"] ?? "http://localhost:8787",
        changeOrigin: false,
      },
    },
  },
  build: {
    target: "es2022",
    outDir: "dist",
    assetsDir: "assets",
    // No data: URI: img-src and font-src are 'self' only.
    assetsInlineLimit: 0,
    // The polyfill would need an inline script; every supported browser has
    // <link rel="modulepreload">.
    modulePreload: { polyfill: false },
    sourcemap: false,
    emptyOutDir: true,
  },
  test: {
    environment: "jsdom",
    globals: false,
    setupFiles: ["./test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}", "test/**/*.test.ts", "e2e/**/*.test.ts"],
    restoreMocks: true,
    coverage: {
      provider: "v8",
      reportsDirectory: "coverage",
      reporter: ["text", "lcov"],
      include: ["src/**"],
      exclude: ["src/api/schema.d.ts", "src/main.tsx", "src/vite-env.d.ts", "src/i18n/i18next.d.ts"],
      thresholds: { lines: 80, statements: 80, functions: 80, branches: 80 },
    },
  },
});
