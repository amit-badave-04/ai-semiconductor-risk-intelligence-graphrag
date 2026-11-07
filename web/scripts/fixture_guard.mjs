// Guards for the fixture server that web/scripts/lighthouse.mjs starts (docs/specs/m5b-frontend.md S13).
//
// The runner spawns the guarded fixture server (tests/e2e/serve_fixture_app.py) on a fixed loopback port and then waits for
// an HTTP answer. Without these checks, a server that already held the port (say a dev server running with real settings)
// made the fixture server fail to bind and exit, while the wait still succeeded against the stranger and Lighthouse loaded
// its pages. So:
//   (a) the port must be free before the child is spawned: a TCP connect to it must fail with "connection refused"; a connect
//       that succeeds, times out or fails any other way is NOT proof of a free port, and the run is refused (fail closed);
//   (b) the wait for readiness fails as soon as the spawned child has exited (or could not be spawned), and an answer is never
//       accepted for a child that is already dead;
//   (c) an explicitly given server (--base <url>) is used as it is: no port check, nothing spawned;
//   (d) the stop returns only when the server is gone (the child has exited and the port refuses connections), so that the next
//       run on the same port is not refused by this run's server and no server outlives the run;
//   (e) a SIGINT, SIGTERM or SIGHUP at ANY point of a run (also during the readiness wait, and while a Lighthouse run is going) stops the
//       server and the running Lighthouse child, and the run exits with the signal's code and no other (installShutdown).
//
// Node built-ins only, no top-level side effects: tests/ui/lighthouse_guard.test.mjs runs under `node --test` in CI's unit job,
// where web/node_modules does not exist.
import { spawn, spawnSync } from "node:child_process";
import net from "node:net";

const DEFAULT_HOST = "127.0.0.1";
const DEFAULT_READY_PATH = "/api/config";

function assertPortNumber(port) {
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new TypeError(`port must be an integer from 1 to 65535, got ${String(port)}`);
}

// Resolves to {state: "open"} (something accepted the connection), {state: "closed"} (connection refused) or
// {state: "unknown", detail} (any other error, or no answer within timeoutMs). It never rejects.
function probePort(port, host, timeoutMs, connect) {
  return new Promise((resolve) => {
    let settled = false;
    let timer;
    let socket;
    const done = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      socket?.destroy();
      resolve(result);
    };
    timer = setTimeout(() => done({ state: "unknown", detail: `no answer within ${timeoutMs} ms` }), timeoutMs);
    try {
      socket = connect({ port, host });
    } catch (err) {
      done({ state: "unknown", detail: err.code ?? err.message });
      return;
    }
    socket.once("connect", () => done({ state: "open" }));
    socket.once("error", (err) => done(err.code === "ECONNREFUSED" ? { state: "closed" } : { state: "unknown", detail: err.code ?? err.message }));
  });
}

// Rejects unless a TCP connect to host:port is refused. `connect` is injectable for the tests only.
export async function assertPortFree(port, { host = DEFAULT_HOST, timeoutMs = 5_000, connect = net.connect } = {}) {
  assertPortNumber(port);
  const probe = await probePort(port, host, timeoutMs, connect);
  if (probe.state === "closed") return;
  if (probe.state === "open") {
    throw new Error(
      `port ${port} on ${host} is already in use, so the guarded fixture server cannot bind it and whatever answers there is not the ` +
        "fixture server. Stop that process, or pass --base <url> to audit a server you started on purpose.",
    );
  }
  throw new Error(`could not confirm that port ${port} on ${host} is free (${probe.detail}); refusing to start the fixture server there.`);
}

const hasExited = (child) => child.exitCode !== null || child.signalCode !== null;
const describeExit = (child) => (child.signalCode !== null ? `signal ${child.signalCode}` : `code ${child.exitCode}`);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ---- stopping the server
//
// A stop that only sends a signal returns before anything has died. The CI web job runs `npm run lighthouse` and then
// `npm run lighthouse -- --legacy` on one port, so the second run's assertPortFree could still find the first run's server; and on
// POSIX the signal reached `uv` only, so the python it had started could go on listening after the script had exited.
// stopServerProcess returns only when the server is GONE: the child has exited and the port refuses connections.

// Spawn options that make the child lead a process group of its own, so that the stop can signal the whole group (uv and the
// python it runs). POSIX only: on Windows `detached` opens a console window, and taskkill /T walks the process tree anyway.
export function groupLeaderOptions(platform = process.platform) {
  return platform === "win32" ? {} : { detached: true };
}

const TASKKILL_TIMEOUT_MS = 10_000;
const killByPid = (pid, signal) => process.kill(pid, signal);

// Windows: taskkill /T /F ends the child and everything it started (it is a kill, not a request, so SIGTERM and SIGKILL are the
// same), and is bounded by a timeout like every other wait of the stop. It never runs for a child that Node has already seen exit:
// that pid may by now belong to an unrelated process, and /T would end that process and its tree.
// POSIX: the signal goes to the child's process group, so it reaches every process in it, also after the leader is gone (the group
// id is not reused while a member lives); a child that leads no group (ESRCH: it was not spawned with groupLeaderOptions) gets the
// signal itself. `spawnSyncImpl` and `killImpl` are injectable for the tests only.
function signalTree(child, signal, platform, { spawnSyncImpl, killImpl }) {
  if (platform === "win32") {
    if (hasExited(child)) return;
    spawnSyncImpl("taskkill", ["/pid", String(child.pid), "/T", "/F"], { stdio: "ignore", timeout: TASKKILL_TIMEOUT_MS });
    return;
  }
  try {
    killImpl(-child.pid, signal);
  } catch (err) {
    if (err.code !== "ESRCH") throw err;
    if (!hasExited(child)) child.kill(signal);
  }
}

// Resolves true once the child has exited (at once when it already has), false after ms. The timer never outlives the wait.
function exitedWithin(child, ms) {
  return new Promise((resolve) => {
    if (hasExited(child)) {
      resolve(true);
      return;
    }
    const onExit = () => {
      clearTimeout(timer);
      resolve(true);
    };
    const timer = setTimeout(() => {
      child.off("exit", onExit);
      resolve(false);
    }, ms);
    child.once("exit", onExit);
  });
}

// Polls until a connect to host:port is refused, or ms have passed; resolves to the last probe ({state: "closed"} on success).
async function refusedWithin(port, host, ms, pollMs) {
  const deadline = Date.now() + ms;
  for (;;) {
    const probe = await probePort(port, host, Math.max(250, Math.min(1_000, deadline - Date.now())), net.connect);
    if (probe.state === "closed" || Date.now() >= deadline) return probe;
    await sleep(Math.min(pollMs, Math.max(0, deadline - Date.now())));
  }
}

// null once the child has exited and (when a port is given) that port refuses connections, within ms in all; otherwise why not.
async function whyNotGone(child, { port, host, ms, pollMs }) {
  const deadline = Date.now() + ms;
  if (!(await exitedWithin(child, ms))) return `the server process (pid ${child.pid}) has not exited`;
  if (port === null) return null;
  const probe = await refusedWithin(port, host, Math.max(0, deadline - Date.now()), pollMs);
  if (probe.state === "closed") return null;
  return probe.state === "open"
    ? `port ${port} on ${host} still accepts connections`
    : `port ${port} on ${host} could not be confirmed free (${probe.detail})`;
}

// Stops a spawned server and its whole tree, and resolves only when it is gone. Resolves at once when the child has already
// exited (or never started). Every wait is bounded:
//   1. SIGTERM to the process group (POSIX) / taskkill /T /F (Windows, itself bounded by a timeout), then up to graceMs for the exit
//      and a refused connect;
//   2. SIGKILL to the group (POSIX; on Windows the same taskkill again, but only while the child has not exited: a child that is gone
//      has a pid that may belong to someone else by now, and the orphans it left are out of taskkill's reach), then up to killMs;
//   3. still not gone: rejects with the reason (a process that did not exit, or a port that still answers).
// `port` is the port the server listens on; without it only the exit is awaited. `spawnSyncImpl` and `killImpl` are for the tests.
export async function stopServerProcess(child, {
  port = null, host = DEFAULT_HOST, graceMs = 10_000, killMs = 10_000, pollMs = 50, platform = process.platform,
  spawnSyncImpl = spawnSync, killImpl = killByPid,
} = {}) {
  if (!child || child.pid === undefined || hasExited(child)) return;
  if (port !== null) assertPortNumber(port);
  const wait = (ms) => whyNotGone(child, { port, host, ms, pollMs });
  const runners = { spawnSyncImpl, killImpl };
  signalTree(child, "SIGTERM", platform, runners);
  if ((await wait(graceMs)) === null) return;
  signalTree(child, "SIGKILL", platform, runners);
  const problem = await wait(killMs);
  if (problem === null) return;
  const how = platform === "win32" ? "taskkill /T /F" : "SIGTERM, then SIGKILL, to its process group";
  throw new Error(`could not stop the fixture server: ${problem}, ${graceMs + killMs} ms after it was told to stop (${how})`);
}

// Resolves once `url` answers 2xx. With a `child` (a spawned ChildProcess) it rejects as soon as the child has exited or
// failed to spawn, whether that happens before, during or just after an answer; the state is read from the child itself,
// not only from an 'exit' event, so a child that died before this was called is still seen. Without a child it only waits.
export async function waitForReady(url, { ms = 180_000, intervalMs = 500, child = null } = {}) {
  const deadline = Date.now() + ms;
  let spawnError = null;
  const onError = (err) => {
    spawnError = err;
  };
  child?.on("error", onError);
  const assertChildAlive = () => {
    if (!child) return;
    if (spawnError) throw new Error(`the server process could not be started: ${spawnError.message}`);
    if (hasExited(child)) throw new Error(`the server process exited (${describeExit(child)}) before ${url} answered`);
  };
  try {
    while (Date.now() < deadline) {
      assertChildAlive();
      let answered = false;
      try {
        answered = (await fetch(url, { signal: AbortSignal.timeout(Math.max(1, Math.min(5_000, deadline - Date.now()))) })).ok;
      } catch {
        /* not up yet */
      }
      assertChildAlive();
      if (answered) return;
      await sleep(intervalMs);
    }
    assertChildAlive();
    throw new Error(`${url} did not answer within ${ms} ms`);
  } finally {
    child?.off("error", onError);
  }
}

// Brings the server up for one run and returns {base, server}; server is null for an explicit base.
//   explicitBase  an already running server named by the caller (--base): waited for, never spawned, port never checked
//   otherwise     assertPortFree(port), THEN start() (spawns the child), then waitForReady with the child; when the wait fails the
//                 child is stopped with `await stop(child)` (unless it has already exited) before the error is rethrown; a stop that
//                 fails is named in the error that is thrown, next to the reason the bring-up failed
export async function bringUpServer({
  explicitBase, port, start, stop, host = DEFAULT_HOST, readyPath = DEFAULT_READY_PATH, readyMs = 180_000, intervalMs = 500,
}) {
  if (explicitBase) {
    await waitForReady(`${explicitBase}${readyPath}`, { ms: readyMs, intervalMs });
    return { base: explicitBase, server: null };
  }
  await assertPortFree(port, { host });
  const base = `http://${host}:${port}`;
  const server = start();
  try {
    await waitForReady(`${base}${readyPath}`, { ms: readyMs, intervalMs, child: server });
  } catch (err) {
    if (!hasExited(server)) {
      try {
        await stop(server);
      } catch (stopErr) {
        throw new Error(`${err.message}; and the server could not be stopped: ${stopErr.message}`, { cause: err });
      }
    }
    throw err;
  }
  return { base, server };
}

// ---- the end of a run: signals, one stop, a run that can be killed
//
// On POSIX the server is spawned as a process group of its own (groupLeaderOptions), so a Ctrl-C or a CI cancel that reaches the
// script no longer reaches the server by itself. Handlers that were installed only after the bring-up left a window of up to 180 s
// (the readiness wait) in which a signal ended node and orphaned the server on its port, and a Lighthouse run that was a spawnSync
// kept node from handling any signal until all ten runs had finished. So installShutdown is called BEFORE the server is brought up,
// over a stopServer function that reads the variable start() sets, and it provides the rest of what the end of a run needs:
//   stop()            ONE stop: the first call starts it, every later call (the failed bring-up, the finally, the signal handler) gets
//                     the same promise, so two stops never run at once. It stops the server and every child started with run().
//   run(cmd, args)    spawn (async, not spawnSync) a child that stop() can kill: resolves {status, signal, stdout, stderr} when it has
//                     closed, rejects when it cannot be started or when a signal has already been received; the promise's `.child`
//                     is the ChildProcess. POSIX: the child leads a process group of its own, like the server.
//   received()        null, or {signal, code} of the FIRST signal received
//   unlessSignalled() resolves at once; after a signal it NEVER resolves: the handler is stopping everything and will exit with the
//                     signal's code, so the code that comes after a killed step must not report anything or exit by itself. Without
//                     it a killed run could print "gates passed", or fail with exit code 1, before the handler's exit(143).
//   uninstall()       removes the handlers
// A second signal while the first is being handled is ignored (the stop is bounded: graceMs + killMs per process, in parallel); the
// exit code is the first signal's. A stop that fails is logged and the exit code is still the signal's.
// `proc`, `exit`, `log` and `signals` are injectable for the tests only.
// SIGHUP (a closed terminal, a dropped ssh session) needs a handler for the same reason the rest of this section exists: on POSIX the
// server and the Lighthouse child lead sessions of their own (groupLeaderOptions), so they never receive the terminal's SIGHUP
// themselves, and without a handler node would die of it and leave both running. SIGQUIT is left alone on purpose: handling it
// would suppress the core dump of a Ctrl-backslash.
const SIGNAL_EXIT_CODES = Object.freeze({ SIGINT: 130, SIGTERM: 143, SIGHUP: 129 });
const MAX_OUTPUT_CHARS = 1_000_000;
const keepTail = (text) => (text.length > MAX_OUTPUT_CHARS ? text.slice(-MAX_OUTPUT_CHARS) : text);

export function installShutdown({
  stopServer = () => undefined, stopOptions = {}, proc = process, exit = (code) => process.exit(code),
  log = (line) => console.error(line), signals = SIGNAL_EXIT_CODES,
} = {}) {
  const running = new Set();
  let stopping = null;
  let received = null;
  let handling = null;

  const stop = () => {
    stopping ??= (async () => {
      const outcomes = await Promise.allSettled([
        Promise.resolve().then(() => stopServer()),
        ...[...running].map((child) => stopServerProcess(child, stopOptions)),
      ]);
      const reasons = outcomes.filter((outcome) => outcome.status === "rejected").map((outcome) => outcome.reason?.message ?? String(outcome.reason));
      if (reasons.length > 0) throw new Error(reasons.join("; "));
    })();
    return stopping;
  };

  const run = (command, args, { env, cwd } = {}) => {
    if (received) return Promise.reject(new Error(`not started: ${received.signal} has been received`));
    if (stopping) return Promise.reject(new Error("not started: the fixture server is being stopped"));
    let child;
    const finished = new Promise((resolve, reject) => {
      child = spawn(command, args, { env, cwd, stdio: ["ignore", "pipe", "pipe"], ...groupLeaderOptions() });
      running.add(child);
      let stdout = "";
      let stderr = "";
      child.stdout.setEncoding("utf8").on("data", (chunk) => { stdout = keepTail(stdout + chunk); });
      child.stderr.setEncoding("utf8").on("data", (chunk) => { stderr = keepTail(stderr + chunk); });
      child.once("error", (err) => {
        running.delete(child);
        reject(err);
      });
      child.once("close", (status, signal) => {
        running.delete(child);
        resolve({ status, signal, stdout, stderr });
      });
    });
    finished.child = child;
    return finished;
  };

  const handle = (signal) => {
    received ??= Object.freeze({ signal, code: signals[signal] });
    handling ??= (async () => {
      log(`lighthouse: ${signal} received, stopping the fixture server and any running audit before exit`);
      try {
        await stop();
      } catch (err) {
        log(`lighthouse: ${err.message}`);
      }
      exit(received.code);
    })();
  };
  const listeners = Object.keys(signals).map((signal) => [signal, () => handle(signal)]);
  for (const [signal, listener] of listeners) proc.on(signal, listener);

  return {
    stop,
    run,
    received: () => received,
    unlessSignalled: () => (received ? new Promise(() => {}) : Promise.resolve()),
    uninstall: () => {
      for (const [signal, listener] of listeners) proc.off(signal, listener);
    },
  };
}
