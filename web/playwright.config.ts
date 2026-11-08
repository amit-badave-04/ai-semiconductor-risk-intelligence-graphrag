import path from "node:path";
import { defineConfig, devices } from "@playwright/test";
import { DUMMY_SITE_KEY, FLIPPED, FLIPPED_PORT, PREVIEW, PREVIEW_PORT, SITE_KEY, SITE_KEY_PORT } from "./e2e/servers.ts";

// The fixture server is Python (tests/e2e/serve_fixture_app.py), started from the repository root with the built app
// (`uv run --no-sync`: the environment was synced by the caller, and a test run must not change it).
// REPO_ROOT and WEB_DIST let a checkout whose web/ was copied elsewhere (for example off OneDrive) point at the real ones.
const repoRoot = process.env.REPO_ROOT ?? path.resolve(import.meta.dirname, "..");
const dist = process.env.WEB_DIST ?? path.join(repoRoot, "web", "dist");
const serve = (port: number, flag: string) =>
  `uv run --no-sync python tests/e2e/serve_fixture_app.py --dist "${dist}" --port ${port} ${flag}`;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: { baseURL: PREVIEW, trace: "retain-on-failure" },
  // Fail closed (turnstile.spec.ts proves it): the browser resolves challenges.cloudflare.com to nothing, so a spec that forgets
  // stubTurnstile can never load the real script or meet a real challenge. page.route (stubTurnstile) fulfils a request before
  // any name resolution, so a stubbed page is unaffected.
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], launchOptions: { args: ["--host-resolver-rules=MAP challenges.cloudflare.com ~NOTFOUND"] } },
    },
  ],
  // Never reuse a server that is already on the port (locally or in CI): it could be the real-settings app, and the tests
  // must only ever talk to the guarded fixture server started here. Playwright then fails fast if the port is taken.
  webServer: [
    { command: serve(PREVIEW_PORT, "--ui-v2-preview --agent"), cwd: repoRoot, url: `${PREVIEW}/api/config`, reuseExistingServer: false, timeout: 180_000 },
    { command: serve(FLIPPED_PORT, "--ui-v2"), cwd: repoRoot, url: `${FLIPPED}/api/config`, reuseExistingServer: false, timeout: 180_000 },
    // the Turnstile specs: a dummy site key (the Cloudflare script is stubbed at the network layer) and a stubbed bot check
    {
      command: serve(SITE_KEY_PORT, `--ui-v2-preview --site-key ${DUMMY_SITE_KEY} --turnstile-stub`),
      cwd: repoRoot,
      url: `${SITE_KEY}/api/config`,
      reuseExistingServer: false,
      timeout: 180_000,
    },
  ],
});
