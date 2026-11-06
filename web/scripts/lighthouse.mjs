// Lighthouse against the fixture server (docs/specs/m5b-frontend.md Acceptance, B3, S8).
//
//   npm run lighthouse                 the five /v2/ pages x mobile and desktop = ten runs; accessibility >= 95 and
//                                      best-practices >= 90 are GATES, performance and SEO are recorded
//   npm run lighthouse -- --legacy     the S8 baseline: the old page at /legacy, mobile and desktop, recorded, never gated
//   options: --out <dir> (default lighthouse-out)   --base <url> (use a server that is already running)
//
// The fixture server (tests/e2e/serve_fixture_app.py) is started and stopped here unless --base is given. Chromium is
// Playwright's, so no system Chrome is needed. REPO_ROOT and WEB_DIST work as in playwright.config.ts.
import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { chromium } from "@playwright/test";

const require = createRequire(import.meta.url);
const webDir = path.resolve(import.meta.dirname, "..");
const repoRoot = process.env.REPO_ROOT ?? path.resolve(webDir, "..");
const dist = process.env.WEB_DIST ?? path.join(repoRoot, "web", "dist");
const args = process.argv.slice(2);
const flag = (name) => args.includes(name);
const option = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);

const LEGACY = flag("--legacy");
const OUT = path.resolve(webDir, option("--out", "lighthouse-out"));
const PORT = 8767;
const GATES = { accessibility: 0.95, "best-practices": 0.9 };
const PAGES = LEGACY ? ["/legacy"] : ["/v2/", "/v2/company/NVDA/", "/v2/workspace/", "/v2/freshness/", "/v2/method/"];
const FORM_FACTORS = ["mobile", "desktop"];
const CATEGORIES = "accessibility,best-practices,performance,seo";

async function waitFor(url, ms) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    try {
      if ((await fetch(url)).ok) return;
    } catch {
      /* not up yet */
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`${url} did not answer within ${ms} ms`);
}

function startServer() {
  return spawn(
    process.platform === "win32" ? "uv.exe" : "uv",
    ["run", "--no-sync", "python", "tests/e2e/serve_fixture_app.py", "--dist", dist, "--port", String(PORT), "--ui-v2-preview"],
    { cwd: repoRoot, stdio: ["ignore", "inherit", "inherit"] },
  );
}

function stopServer(server) {
  if (process.platform === "win32") spawnSync("taskkill", ["/pid", String(server.pid), "/T", "/F"]);
  else server.kill("SIGTERM");
}

function lighthouseBin() {
  const pkg = JSON.parse(readFileSync(require.resolve("lighthouse/package.json"), "utf8"));
  const bin = typeof pkg.bin === "string" ? pkg.bin : pkg.bin.lighthouse;
  return path.join(path.dirname(require.resolve("lighthouse/package.json")), bin);
}

function run(url, formFactor, file) {
  const cli = [
    lighthouseBin(), url, "--output=json", `--output-path=${file}`, `--only-categories=${CATEGORIES}`, "--quiet",
    "--chrome-flags=--headless=new --no-sandbox --disable-gpu",
    ...(formFactor === "desktop" ? ["--preset=desktop"] : []),
  ];
  const result = spawnSync(process.execPath, cli, { env: { ...process.env, CHROME_PATH: chromium.executablePath() }, encoding: "utf8" });
  if (result.status !== 0) throw new Error(`lighthouse failed for ${url} (${formFactor}): ${result.stderr || result.stdout}`);
  const lhr = JSON.parse(readFileSync(file, "utf8"));
  const requests = lhr.audits["network-requests"]?.details?.items ?? [];
  return {
    url, formFactor, lighthouseVersion: lhr.lighthouseVersion,
    scores: Object.fromEntries(Object.entries(lhr.categories).map(([id, c]) => [id, c.score])),
    requests: requests.length, transferBytes: requests.reduce((sum, r) => sum + (r.transferSize ?? 0), 0),
    runtimeError: lhr.runtimeError ?? null,
  };
}

mkdirSync(OUT, { recursive: true });
const base = option("--base", `http://127.0.0.1:${PORT}`);
const server = option("--base") ? null : startServer();
const results = [];
try {
  await waitFor(`${base}/api/config`, 180_000);
  for (const page of PAGES) {
    for (const formFactor of FORM_FACTORS) {
      const name = `${page.replace(/[^a-z0-9]+/gi, "_")}.${formFactor}.json`;
      const row = run(base + page, formFactor, path.join(OUT, name));
      results.push(row);
      console.log(`${formFactor.padEnd(7)} ${page.padEnd(20)} a11y ${row.scores.accessibility} best-practices ${row.scores["best-practices"]} perf ${row.scores.performance} seo ${row.scores.seo} (${row.requests} requests)`);
    }
  }
} finally {
  if (server) stopServer(server);
}

const failures = LEGACY
  ? []
  : results.flatMap((r) => Object.entries(GATES).filter(([id, min]) => !(r.scores[id] >= min)).map(([id, min]) => `${r.url} ${r.formFactor}: ${id} ${r.scores[id]} < ${min}`));
const summary = { gated: !LEGACY, gates: LEGACY ? null : GATES, failures, results };
writeFileSync(path.join(OUT, LEGACY ? "summary.legacy.json" : "summary.json"), `${JSON.stringify(summary, null, 2)}\n`);
if (failures.length) {
  console.error(`Lighthouse gate failed:\n${failures.join("\n")}`);
  process.exit(1);
}
console.log(LEGACY ? "S8 baseline recorded (not gated)." : "Lighthouse gates passed.");
