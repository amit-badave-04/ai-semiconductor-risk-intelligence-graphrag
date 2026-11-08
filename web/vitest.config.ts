import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Unit tests only: web/e2e/*.spec.ts belongs to Playwright, so the include is explicit.
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "src") } },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["src/test/setup.ts"],
    css: false,
    coverage: {
      provider: "v8",
      reporter: ["text", "json-summary"],
      // Vitest 5: include/exclude match paths relative to the project root, and a bare pattern is a directory.
      include: ["src/**/*.{ts,tsx}"],
      // Excluded: the copied-in shadcn components and the generated constants (docs/specs/m5b-frontend.md Acceptance), plus the test files and setup, which are not product code. Test helpers under __tests__ count.
      exclude: ["src/components/ui/**", "src/**/*.generated.ts", "src/**/*.test.{ts,tsx}", "src/test/**", "src/vite-env.d.ts"],
      thresholds: { statements: 80, branches: 80, functions: 80, lines: 80 },
    },
  },
});
