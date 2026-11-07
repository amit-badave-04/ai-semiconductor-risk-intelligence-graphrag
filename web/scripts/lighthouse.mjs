// Lighthouse against the fixture server (docs/specs/m5b-frontend.md Acceptance, B3, S8).
//
//   npm run lighthouse                 the five /v2/ pages x mobile and desktop = ten runs; accessibility >= 95 and
//                                      best-practices >= 90 are GATES, performance and SEO are recorded
//   npm run lighthouse -- --legacy     the S8 baseline: the old page at /legacy, mobile and desktop, recorded, never gated
//   options: --out <dir> (default lighthouse-out)   --base <url> (use a server that is already running)
//
// The fixture server (tests/e2e/serve_fixture_app.py) is started and stopped here unless --base is given. Chromium is
// Playwright's, so no system Chrome is needed. REPO_ROOT and WEB_DIST work as in playwright.config.ts.
// The runner refuses to start when the fixture port is already taken (whatever answered there would not be the guarded server) and
// stops when the server process exits before it answers; --base <url> names a server you started on purpose and is used as given.
// The server is stopped with its whole process tree and the script exits only after it is gone (port free), so two runs back to back
// (CI runs the gated one and then --legacy) can share the port.
// A SIGINT, SIGTERM or SIGHUP at any point (the readiness wait, a Lighthouse run, the final stop) stops the server and the running
// Lighthouse child and exits with 130, 143 or 129; the handlers are installed before the server is brought up, and Lighthouse runs asynchronously so
// that they can fire during a run.
// The checks are in fixture_guard.mjs (tests/ui/lighthouse_guard.test.mjs).
import { spawn } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { chromium } from "@playwright/test";
import { bringUpServer, groupLeaderOptions, installShutdown, stopServerProcess } from "./fixture_guard.mjs";

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

// The server has a process group of its own on POSIX, so a Ctrl-C or a CI cancel that reaches this script does not reach the server
// by itself. The shutdown owns that: the signal handlers (installed here, BEFORE the server is brought up and over `server`, which
// startServer sets), ONE memoized stop that the failed bring-up, the finally and the handler all share, and the Lighthouse child.
let server = null;
const shutdown = installShutdown({
  // Resolves only when the server is gone: its process tree has exited and PORT refuses connections (fixture_guard.mjs).
  stopServer: () => (server ? stopServerProcess(server, { port: PORT }) : undefined),
});

function startServer() {
  const signal = shutdown.received();
  if (signal) throw new Error(`the fixture server was not started: ${signal.signal} has been received`);
  server = spawn(
    process.platform === "win32" ? "uv.exe" : "uv",
    ["run", "--no-sync", "python", "tests/e2e/serve_fixture_app.py", "--dist", dist, "--port", String(PORT), "--ui-v2-preview"],
    // POSIX: the server leads a process group of its own, so the stop can signal uv and the python it runs together
    { cwd: repoRoot, stdio: ["ignore", "inherit", "inherit"], ...groupLeaderOptions() },
  );
  return server;
}

function lighthouseBin() {
  const pkg = JSON.parse(readFileSync(require.resolve("lighthouse/package.json"), "utf8"));
  const bin = typeof pkg.bin === "string" ? pkg.bin : pkg.bin.lighthouse;
  return path.join(path.dirname(require.resolve("lighthouse/package.json")), bin);
}

// Asynchronous, through the shutdown: a signal that arrives during a run kills this child and does not wait for the run to end.
async function run(url, formFactor, file) {
  const cli = [
    lighthouseBin(), url, "--output=json", `--output-path=${file}`, `--only-categories=${CATEGORIES}`, "--quiet",
    "--chrome-flags=--headless=new --no-sandbox --disable-gpu",
    ...(formFactor === "desktop" ? ["--preset=desktop"] : []),
  ];
  const result = await shutdown.run(process.execPath, cli, { env: { ...process.env, CHROME_PATH: chromium.executablePath() } });
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
// With --base the named server is used as given: nothing is spawned and the port is not checked. Without it the port must be free
// BEFORE the fixture server is spawned, and the wait fails as soon as that server exits. A failure has already stopped the child
// (awaited: the child has exited and the port is free again by the time the failure is reported).
let base;
try {
  ({ base } = await bringUpServer({ explicitBase: option("--base"), port: PORT, start: startServer, stop: shutdown.stop }));
} catch (err) {
  // A signal that arrived during the bring-up stopped the server and ended the wait: the handler exits with the signal's code, and
  // this error (the server process exited) is its consequence, not a failure to report or an exit code of its own.
  await shutdown.unlessSignalled();
  console.error(`lighthouse: ${err.message}`);
  process.exit(1);
}
const results = [];
let stopFailure = null;
try {
  for (const page of PAGES) {
    for (const formFactor of FORM_FACTORS) {
      const name = `${page.replace(/[^a-z0-9]+/gi, "_")}.${formFactor}.json`;
      const row = await run(base + page, formFactor, path.join(OUT, name));
      results.push(row);
      console.log(`${formFactor.padEnd(7)} ${page.padEnd(20)} a11y ${row.scores.accessibility} best-practices ${row.scores["best-practices"]} perf ${row.scores.performance} seo ${row.scores.seo} (${row.requests} requests)`);
    }
  }
} catch (err) {
  // A signal kills the running Lighthouse; that is not a Lighthouse failure, and the handler owns the exit code.
  await shutdown.unlessSignalled();
  throw err;
} finally {
  // Awaited: the script must not go on, or exit, while the server is still up, or the next run on this port is refused. The stop is
  // the one the signal handler shares, never a second one. A stop that fails is reported and fails the run below; it never replaces
  // an error thrown by a Lighthouse run. After a signal the handler reports the failure, so it is not reported twice.
  try {
    await shutdown.stop();
  } catch (err) {
    stopFailure = err;
    if (!shutdown.received()) console.error(`lighthouse: ${err.message}`);
  }
}
// A signal that arrived during the stop: the handler exits with its code, so there is no summary and no success line to disagree with it.
await shutdown.unlessSignalled();

const failures = LEGACY
  ? []
  : results.flatMap((r) => Object.entries(GATES).filter(([id, min]) => !(r.scores[id] >= min)).map(([id, min]) => `${r.url} ${r.formFactor}: ${id} ${r.scores[id]} < ${min}`));
const summary = { gated: !LEGACY, gates: LEGACY ? null : GATES, failures, results };
writeFileSync(path.join(OUT, LEGACY ? "summary.legacy.json" : "summary.json"), `${JSON.stringify(summary, null, 2)}\n`);
if (failures.length) {
  console.error(`Lighthouse gate failed:\n${failures.join("\n")}`);
  process.exit(1);
}
if (stopFailure) process.exit(1);   // the message is above; the gates were met, but the server is not known to be gone
console.log(LEGACY ? "S8 baseline recorded (not gated)." : "Lighthouse gates passed.");
