import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// ONE artifact (docs/specs/m5b-frontend.md R12): the base is the absolute /v2/ and the router basename is chosen at runtime
// from the path (src/lib/basename.ts), so the same dist serves /v2/ and, once UI_V2 flips the root, /. The server's CSP has
// base-uri 'none', so a <base> element is not an option.
// index.html preloads only the regular sans face (docs/specs/m5b-redesign.md RD2); Vite rewrites its href to the hashed file.
const FONT_FILE = /\.(?:woff2?|ttf|otf|eot)$/i;

export default defineConfig({
  base: "/v2/",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "src") } },
  build: {
    // The CSP has no font-src, so a data: font is blocked: a font file is never inlined (R4). Images may be (img-src has data:).
    assetsInlineLimit: (file) => (FONT_FILE.test(file) ? false : undefined),
  },
  server: {
    // `npm run dev` with a local uvicorn (see README.md): the API is same-origin in production, so it is proxied here.
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
