import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` proxies /api to a locally running `kestrel api` (127.0.0.1:8710).
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8710" } },
  build: { outDir: "dist", sourcemap: false },
});
