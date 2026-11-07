// The Lighthouse runner's fixture-server guard (web/scripts/fixture_guard.mjs, used by web/scripts/lighthouse.mjs).
//
// The runner starts tests/e2e/serve_fixture_app.py on a fixed loopback port and then waits for an HTTP answer. If something
// else already held the port (a dev server running with real settings), the fixture server could not bind and exited, the
// wait still succeeded against the stranger, and Lighthouse audited it. These tests pin the three rules that stop that:
//   (a) the port must be free before the child is spawned (and the child is not spawned when it is not),
//   (b) the wait fails as soon as the child has exited, and never returns ready for a dead child,
//   (c) an explicitly given --base server is used as it is: no port check, nothing spawned.
// And one rule for the end of a run (CI runs `npm run lighthouse` and then `npm run lighthouse -- --legacy` on the same port):
//   (d) the server is gone when the stop has returned: the stop helper waits for the child to exit and for the port to refuse
//       connections (POSIX: it signals the child's whole process group and escalates to SIGKILL), every wait is bounded, and
//       bringUpServer awaits the stop before it rethrows.
// Node built-ins only (CI's unit job has no web/node_modules). Every port is ephemeral; the tests never use 8767 to listen.
// Over the 800-line guideline on purpose: one file keeps every exit path of the guard (each signal, each stop race, each
// platform) next to the stub server and the helpers they share.
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { EventEmitter } from "node:events";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import http from "node:http";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import * as guard from "../../web/scripts/fixture_guard.mjs";
// A namespace import for the whole module: a missing export then fails the one test that uses it, not the load of the file.
const { assertPortFree, bringUpServer, groupLeaderOptions, installShutdown, stopServerProcess, waitForReady } = guard;
const GUARD_URL = new URL("../../web/scripts/fixture_guard.mjs", import.meta.url).href;
const LIGHTHOUSE_SOURCE = new URL("../../web/scripts/lighthouse.mjs", import.meta.url);

const NODE = process.execPath;
const FAST = { ms: 3_000, intervalMs: 25 };
const SOME_PORT = 8767;   // only ever passed to an injected connect(), never bound or dialled

// ---- helpers: a port nobody holds, a port a test holds, an HTTP server that answers, children that live or die

async function freePort() {
  const server = net.createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  await new Promise((resolve) => server.close(resolve));
  return port;
}

async function holdPort(t, handler) {
  const server = handler ? http.createServer(handler) : net.createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => { server.closeAllConnections?.(); server.close(resolve); }));
  return server.address().port;
}

const answerOk = (_req, res) => { res.statusCode = 200; res.end("{}"); };

function liveChild(t, source = "setInterval(() => {}, 1000)") {
  const child = spawn(NODE, ["-e", source], { stdio: "ignore" });
  t.after(() => { child.kill(); });
  return child;
}

function deadChild(t, code = 3) {
  const child = spawn(NODE, ["-e", `process.exit(${code})`], { stdio: "ignore" });
  t.after(() => { child.kill(); });
  return child;
}

const exited = (child) => (child.exitCode !== null ? Promise.resolve() : new Promise((resolve) => child.once("exit", resolve)));

// a child that serves HTTP 200 on `port` until it is killed
const httpChildSource = (port) => `require("node:http").createServer((q, s) => s.end("{}")).listen(${port}, "127.0.0.1")`;

// an injected connect() whose socket fails with `code` (or never answers when code is null)
const fakeConnect = (code) => () => {
  const socket = new EventEmitter();
  socket.destroy = () => {};
  if (code) setImmediate(() => socket.emit("error", Object.assign(new Error(code), { code })));
  return socket;
};

// ---- (a) assertPortFree

test("a port held by another server makes the guard refuse, and the message names the port", async (t) => {
  const port = await holdPort(t);
  await assert.rejects(assertPortFree(port), (err) => {
    assert.match(err.message, new RegExp(`\\b${port}\\b`));
    assert.match(err.message, /already in use/);
    assert.match(err.message, /--base/);
    return true;
  });
});

test("a port held by an HTTP server that answers is refused too", async (t) => {
  const port = await holdPort(t, answerOk);
  await assert.rejects(assertPortFree(port), /already in use/);
});

test("a free port passes the guard", async () => {
  await assertPortFree(await freePort());
});

test("a connect that is neither accepted nor refused is not proof of a free port (fail closed)", async () => {
  for (const code of ["EACCES", "EHOSTUNREACH", "ETIMEDOUT"]) {
    await assert.rejects(assertPortFree(SOME_PORT, { connect: fakeConnect(code) }), (err) => {
      assert.match(err.message, /could not confirm/);
      assert.match(err.message, new RegExp(code));
      assert.match(err.message, new RegExp(`\\b${SOME_PORT}\\b`));
      return true;
    }, code);
  }
});

test("a connect that never answers times out and is refused", async () => {
  await assert.rejects(assertPortFree(SOME_PORT, { connect: fakeConnect(null), timeoutMs: 50 }), /could not confirm.*no answer within 50 ms/);
});

test("a refused connect on an injected socket counts as free", async () => {
  await assertPortFree(SOME_PORT, { connect: fakeConnect("ECONNREFUSED") });
});

test("a port that is not a port number is a programming error, not a free port", async () => {
  for (const bad of [0, -1, 65536, 1.5, "8767", undefined, null]) {
    await assert.rejects(assertPortFree(bad), /port/, String(bad));
  }
});

// ---- (b) waitForReady

test("ready before exit passes: the server answers while the child is alive", async (t) => {
  const port = await holdPort(t, answerOk);
  const child = liveChild(t);
  await waitForReady(`http://127.0.0.1:${port}/api/config`, { ...FAST, child });
});

test("a child that exits before anything answers makes the wait fail at once, with its exit code", async (t) => {
  const url = `http://127.0.0.1:${await freePort()}/api/config`;
  const started = Date.now();
  await assert.rejects(waitForReady(url, { ms: 20_000, intervalMs: 25, child: deadChild(t, 3) }), (err) => {
    assert.match(err.message, /exited/);
    assert.match(err.message, /code 3/);
    assert.match(err.message, /api\/config/);
    return true;
  });
  assert.ok(Date.now() - started < 10_000, "the exit must end the wait, not the deadline");
});

test("a child killed while the wait runs ends the wait, reported as a signal or an exit code", async (t) => {
  const url = `http://127.0.0.1:${await freePort()}/api/config`;
  const child = liveChild(t);
  const wait = waitForReady(url, { ms: 20_000, intervalMs: 25, child });
  setTimeout(() => child.kill("SIGTERM"), 100);
  await assert.rejects(wait, (err) => {
    assert.match(err.message, /exited/);
    assert.match(err.message, /SIGTERM|code /);   // Windows reports a kill as an exit code, not a signal
    return true;
  });
});

test("a stranger answering is not ready when the child it was meant to be is already dead", async (t) => {
  const port = await holdPort(t, answerOk);        // the foreign server: answers 200 on the fixture port
  const child = deadChild(t, 1);
  await exited(child);
  await assert.rejects(waitForReady(`http://127.0.0.1:${port}/api/config`, { ...FAST, child }), /exited/);
});

test("a child that dies while the answer is still in flight does not make the server ready", async (t) => {
  const port = await holdPort(t, (_req, res) => { setTimeout(() => { res.statusCode = 200; res.end("{}"); }, 400); });
  const child = liveChild(t);
  setTimeout(() => child.kill("SIGTERM"), 100);    // dead long before the 200 arrives
  await assert.rejects(waitForReady(`http://127.0.0.1:${port}/api/config`, { ms: 5_000, intervalMs: 25, child }), /exited/);
});

test("a child that cannot be spawned (no such program) fails the wait with the spawn error", async () => {
  const url = `http://127.0.0.1:${await freePort()}/api/config`;
  // spawn and wait in the same tick, as bringUpServer does: the 'error' event fires on the next one
  const child = spawn("definitely-not-a-program-xyz-0451", [], { stdio: "ignore" });
  await assert.rejects(waitForReady(url, { ms: 20_000, intervalMs: 25, child }), (err) => {
    assert.match(err.message, /could not be started|exited/);
    assert.match(err.message, /ENOENT|code -/);
    return true;
  });
});

test("with no child the wait is the plain one: it passes once something answers", async (t) => {
  const port = await holdPort(t, answerOk);
  await waitForReady(`http://127.0.0.1:${port}/api/config`, FAST);
});

test("with no child and nothing answering the wait ends at its deadline and names the URL", async () => {
  const url = `http://127.0.0.1:${await freePort()}/api/config`;
  await assert.rejects(waitForReady(url, { ms: 300, intervalMs: 25 }), (err) => {
    assert.match(err.message, /did not answer within 300 ms/);
    assert.match(err.message, /api\/config/);
    return true;
  });
});

test("a server that answers with an error status is not ready", async (t) => {
  const port = await holdPort(t, (_req, res) => { res.statusCode = 503; res.end("no"); });
  await assert.rejects(waitForReady(`http://127.0.0.1:${port}/api/config`, { ms: 300, intervalMs: 25 }), /did not answer/);
});

test("waitForReady leaves no listener behind on the child", async (t) => {
  const port = await holdPort(t, answerOk);
  const child = liveChild(t);
  const before = child.listenerCount("error");
  await waitForReady(`http://127.0.0.1:${port}/api/config`, { ...FAST, child });
  assert.equal(child.listenerCount("error"), before);
});

// ---- bringUpServer: the order and the --base rule

test("a held port refuses the run before anything is spawned", async (t) => {
  const port = await holdPort(t, answerOk);
  let spawned = 0;
  await assert.rejects(
    bringUpServer({ port, start: () => { spawned += 1; return liveChild(t); }, stop: () => {}, readyMs: 500, intervalMs: 25 }),
    (err) => { assert.match(err.message, new RegExp(`\\b${port}\\b`)); return true; },
  );
  assert.equal(spawned, 0, "start() must not be called when the port is taken");
});

test("a free port starts the child and returns the base once the child answers", async (t) => {
  const port = await freePort();
  const { base, server } = await bringUpServer({
    port, start: () => liveChild(t, httpChildSource(port)), stop: (child) => child.kill(), readyMs: 15_000, intervalMs: 25,
  });
  assert.equal(base, `http://127.0.0.1:${port}`);
  assert.equal(typeof server.pid, "number");
  assert.equal(server.exitCode, null);
});

test("a child that exits before it answers fails the run and needs no stop", async (t) => {
  const port = await freePort();
  const stopped = [];
  await assert.rejects(
    bringUpServer({ port, start: () => deadChild(t, 5), stop: (child) => stopped.push(child), readyMs: 20_000, intervalMs: 25 }),
    /exited.*code 5/,
  );
  assert.equal(stopped.length, 0, "an exited child needs no stop");
});

test("a start() whose program does not exist fails the run with the spawn error (no crash on the unhandled 'error')", async () => {
  const port = await freePort();
  await assert.rejects(
    bringUpServer({
      port, start: () => spawn("definitely-not-a-program-xyz-0451", [], { stdio: "ignore" }), stop: () => {}, readyMs: 20_000, intervalMs: 25,
    }),
    /could not be started|exited/,
  );
});

test("a child that never answers fails the run at the deadline and is stopped", async (t) => {
  const port = await freePort();
  const stopped = [];
  let child;
  await assert.rejects(
    bringUpServer({ port, start: () => (child = liveChild(t)), stop: (c) => { stopped.push(c); c.kill(); }, readyMs: 300, intervalMs: 25 }),
    /did not answer within 300 ms/,
  );
  assert.deepEqual(stopped, [child]);
});

test("an explicit --base is used as given: no port check, nothing spawned, no child to stop", async (t) => {
  const heldPort = await holdPort(t, answerOk);    // the fixture port is "taken" and that must not matter
  let spawned = 0;
  const base = `http://127.0.0.1:${heldPort}`;
  const result = await bringUpServer({
    explicitBase: base, port: heldPort, start: () => { spawned += 1; return liveChild(t); }, stop: () => {}, readyMs: 3_000, intervalMs: 25,
  });
  assert.deepEqual(result, { base, server: null });
  assert.equal(spawned, 0);
});

test("an explicit --base that never answers still fails at the deadline, as before", async () => {
  const base = `http://127.0.0.1:${await freePort()}`;
  await assert.rejects(
    bringUpServer({ explicitBase: base, port: SOME_PORT, start: () => assert.fail("must not spawn"), stop: () => {}, readyMs: 300, intervalMs: 25 }),
    /did not answer within 300 ms/,
  );
});

// ---- (d) stopping the server: gone means the child has exited AND the port refuses connections
//
// The old stop sent a signal and returned: on POSIX only to `uv` (the python it started kept listening), and the script exited
// before anything had died, so the next run's assertPortFree could still find the old server. These tests use small node servers
// in place of uv and python, spawned the way lighthouse.mjs spawns the real one (groupLeaderOptions). On Windows `taskkill /T /F`
// ends a server at once, so the cases that need a server that survives SIGTERM or a process group run on POSIX only.

const IS_WINDOWS = process.platform === "win32";
const STOP = { graceMs: 5_000, killMs: 5_000 };      // small and explicit: a hang fails in seconds, far inside the 180 s of the pytest wrapper
const CLOSE_DELAY_MS = 500;
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Every server installs its SIGTERM handling BEFORE it listens, and every test waits until the port answers before it stops the
// child: a SIGTERM that arrives while node is still starting would end the child by default, and the test would prove nothing.
const LISTEN = (port, status = 200) => `require("node:http").createServer((q, s) => { s.statusCode = ${status}; s.end("{}"); }).listen(${port}, "127.0.0.1");`;
const SELF_DESTRUCT = "setTimeout(() => process.exit(0), 20000);";   // a failing test cannot leave a server behind for long
// answers on `port`; after SIGTERM it needs 500 ms (or `delayMs`) to close (a real server finishing its requests)
const slowCloseSource = (port, status, delayMs = CLOSE_DELAY_MS) => `process.on("SIGTERM", () => setTimeout(() => process.exit(0), ${delayMs})); ${SELF_DESTRUCT} ${LISTEN(port, status)}`;
const ignoreTermSource = (port) => `process.on("SIGTERM", () => {}); ${SELF_DESTRUCT} ${LISTEN(port)}`;
// a leader (like uv) that stays alive and starts the source in argv[1] as its child (like python), in the leader's group and tree
const leaderSource = `require("node:child_process").spawn(process.execPath, ["-e", process.argv[1]], { stdio: "ignore" }); ${SELF_DESTRUCT} setInterval(() => {}, 1000);`;
const holderSource = (port) => `${SELF_DESTRUCT} ${LISTEN(port)}`;

// Cleanup that does not depend on the code under test. Windows: no taskkill for a child that has already exited, because its pid
// may have been reused by an unrelated process whose tree /T would then end; the taskkill is bounded like the product's.
// POSIX: the group is signalled even when its leader is gone (a grandchild may still be in it). The two functions are injectable
// so that the tests can pin both rules on any platform.
const killByPid = (pid, signal) => process.kill(pid, signal);

function reap(child, { platform = process.platform, spawnSyncImpl = spawnSync, killImpl = killByPid } = {}) {
  if (child.pid === undefined) return;
  if (platform === "win32") {
    if (!(child.exitCode !== null || child.signalCode !== null)) {
      spawnSyncImpl("taskkill", ["/pid", String(child.pid), "/T", "/F"], { stdio: "ignore", timeout: 10_000 });
    }
  } else {
    try { killImpl(-child.pid, "SIGKILL"); } catch { /* no such group */ }
  }
  try { child.kill("SIGKILL"); } catch { /* gone */ }
}

// spawned like lighthouse.mjs spawns the fixture server (its own process group on POSIX), unless the test says otherwise
function spawnServer(t, source, { args = [], grouped = true } = {}) {
  const child = spawn(NODE, ["-e", source, ...args], { stdio: "ignore", ...(grouped ? groupLeaderOptions() : {}) });
  child.on("error", () => {});
  t.after(() => reap(child));
  return child;
}

const answering = (port) => waitForReady(`http://127.0.0.1:${port}/`, { ms: 15_000, intervalMs: 25 });
const assertExited = (child) => assert.ok(child.exitCode !== null || child.signalCode !== null, "the child must have exited by the time the stop resolves");

test("groupLeaderOptions: a process group of its own on POSIX, today's plain spawn on Windows", () => {
  assert.deepEqual(groupLeaderOptions("win32"), {});
  assert.deepEqual(groupLeaderOptions("linux"), { detached: true });
  assert.deepEqual(groupLeaderOptions("darwin"), { detached: true });
});

test("when the stop resolves, a server that needs 500 ms to close is gone: the child has exited and the port is free", async (t) => {
  const port = await freePort();
  const child = spawnServer(t, slowCloseSource(port));
  await answering(port);
  const started = Date.now();
  await stopServerProcess(child, { port, ...STOP });
  assertExited(child);
  await assertPortFree(port);
  if (!IS_WINDOWS) {
    assert.ok(Date.now() - started >= CLOSE_DELAY_MS - 100, "the stop must have waited for the slow close");
    assert.equal(child.exitCode, 0, "SIGTERM was enough: no escalation to SIGKILL");
  }
});

test("without a port the stop still waits for the child's exit, not only for the signal to be sent", async (t) => {
  const port = await freePort();
  const child = spawnServer(t, slowCloseSource(port));
  await answering(port);
  await stopServerProcess(child, STOP);
  assertExited(child);
});

test("the whole tree is stopped: a grandchild that holds the port goes with its leader", async (t) => {
  const port = await freePort();
  const leader = spawnServer(t, leaderSource, { args: [holderSource(port)] });
  await answering(port);
  await stopServerProcess(leader, { port, ...STOP });
  assertExited(leader);
  await assertPortFree(port);
});

test("a child that has already exited resolves at once, whoever holds the port", async (t) => {
  const heldPort = await holdPort(t, answerOk);
  const child = deadChild(t, 0);
  await exited(child);
  const started = Date.now();
  await stopServerProcess(child, { port: heldPort, graceMs: 30_000, killMs: 30_000 });
  assert.ok(Date.now() - started < 1_000, "an exited child is not waited for");
});

test("a child that could not be spawned resolves at once", async () => {
  const child = spawn("definitely-not-a-program-xyz-0451", [], { stdio: "ignore" });
  child.on("error", () => {});
  const started = Date.now();
  await stopServerProcess(child, { graceMs: 30_000, killMs: 30_000 });
  assert.ok(Date.now() - started < 1_000);
});

test("a server that ignores SIGTERM is killed on its process group once the grace period is over", { skip: IS_WINDOWS && "taskkill /F ends a Windows server at once; SIGTERM and process groups are POSIX" }, async (t) => {
  const port = await freePort();
  const child = spawnServer(t, ignoreTermSource(port));
  await answering(port);
  const started = Date.now();
  await stopServerProcess(child, { port, graceMs: 400, killMs: 5_000 });
  assert.equal(child.signalCode, "SIGKILL");
  assert.ok(Date.now() - started >= 350, "SIGKILL only after the grace period");
  await assertPortFree(port);
});

test("a grandchild that ignores SIGTERM keeps the port after its leader is gone, and is killed through the group", { skip: IS_WINDOWS && "process groups are POSIX" }, async (t) => {
  const port = await freePort();
  const leader = spawnServer(t, leaderSource, { args: [ignoreTermSource(port)] });
  await answering(port);
  await stopServerProcess(leader, { port, graceMs: 400, killMs: 5_000 });
  assertExited(leader);
  await assertPortFree(port);
});

test("a child that was not spawned as a group leader is still stopped", { skip: IS_WINDOWS && "process groups are POSIX" }, async (t) => {
  const port = await freePort();
  const child = spawnServer(t, slowCloseSource(port), { grouped: false });
  await answering(port);
  await stopServerProcess(child, { port, ...STOP });
  assertExited(child);
  await assertPortFree(port);
});

test("a port still held after the child is gone makes the stop reject within its bounds, naming the port", async (t) => {
  const heldPort = await holdPort(t, answerOk);          // a stranger: the child is not the one holding it
  const child = spawnServer(t, "setInterval(() => {}, 1000)");
  const started = Date.now();
  await assert.rejects(stopServerProcess(child, { port: heldPort, graceMs: 300, killMs: 300 }), (err) => {
    assert.match(err.message, /could not stop/);
    assert.match(err.message, new RegExp(`port ${heldPort}\\b`));
    return true;
  });
  assert.ok(Date.now() - started < 10_000, "every wait is bounded");
  assertExited(child);
});

// ---- (d) bringUpServer stops the child before it rethrows

test("when the wait fails, bringUpServer awaits the stop before it rejects", async (t) => {
  const port = await freePort();
  let stopFinished = false;
  await assert.rejects(
    bringUpServer({
      port, start: () => liveChild(t), readyMs: 300, intervalMs: 25,
      stop: async (child) => { await pause(300); child.kill(); stopFinished = true; },
    }),
    /did not answer within 300 ms/,
  );
  assert.equal(stopFinished, true, "the rejection must come after the stop has finished");
});

test("a stop that fails is reported together with the reason the bring-up failed", async (t) => {
  const port = await freePort();
  await assert.rejects(
    bringUpServer({
      port, start: () => liveChild(t), readyMs: 300, intervalMs: 25,
      stop: async () => { throw new Error("pid 4242 did not exit"); },
    }),
    (err) => {
      assert.match(err.message, /did not answer within 300 ms/);
      assert.match(err.message, /pid 4242 did not exit/);
      return true;
    },
  );
});

test("a failed bring-up has stopped the real server by the time it rejects: the port is free again", async (t) => {
  const port = await freePort();
  await assert.rejects(
    bringUpServer({
      port, start: () => spawnServer(t, slowCloseSource(port, 503)), stop: (child) => stopServerProcess(child, { port, ...STOP }),
      readyMs: 600, intervalMs: 25,
    }),
    /did not answer within 600 ms/,
  );
  await assertPortFree(port);
});

test("two runs in a row on one port: the second is not refused by the server of the first", async (t) => {
  const port = await freePort();
  for (let run = 1; run <= 2; run += 1) {
    const { server } = await bringUpServer({
      port, start: () => spawnServer(t, slowCloseSource(port)), stop: (child) => stopServerProcess(child, { port, ...STOP }),
      readyMs: 15_000, intervalMs: 25,
    });
    await stopServerProcess(server, { port, ...STOP });
    assertExited(server);
  }
});

// ---- (d) W2 and S2: the stop never targets a pid that may belong to someone else, and never waits for ever
//
// signalTree and reap() run on Windows against children that Node has already seen exit. By then the pid may belong to an
// unrelated process, and `taskkill /pid <pid> /T /F` would end that process and its tree. The taskkill runner, the kill function and
// the platform are injectable, so these cases run on every platform. The fake child has a pid that no machine hands out: even a
// failed injection could not signal anything real.

const UNUSED_PID = 2_147_483_644;
const TASKKILL_TIMEOUT_MS = 10_000;

function fakeChild({ exited = false } = {}) {
  const child = new EventEmitter();
  child.pid = UNUSED_PID;
  child.exitCode = exited ? 1 : null;
  child.signalCode = null;
  child.kill = () => true;
  return child;
}

const markExited = (child, code = 1) => {
  child.exitCode = code;
  child.emit("exit", code, null);
};

test("W2: a child that exited during the grace period gets no second taskkill (its pid may be reused), and the stop still reports the held port", async (t) => {
  const heldPort = await holdPort(t, answerOk);          // a stranger holds the port, so the stop goes on to the escalation step
  const child = fakeChild();
  const calls = [];
  const spawnSyncImpl = (command, args, options) => {
    calls.push({ command, args, options });
    markExited(child);                                    // taskkill worked: the child is gone, the port is not free
    return { status: 0 };
  };
  await assert.rejects(
    stopServerProcess(child, { port: heldPort, platform: "win32", graceMs: 150, killMs: 150, pollMs: 25, spawnSyncImpl }),
    (err) => {
      assert.match(err.message, /could not stop/);
      assert.match(err.message, new RegExp(`port ${heldPort}\\b`));
      return true;
    },
  );
  assert.equal(calls.length, 1, "taskkill must not run again for a child that has already exited");
  assert.equal(calls[0].command, "taskkill");
  assert.deepEqual(calls[0].args, ["/pid", String(UNUSED_PID), "/T", "/F"]);
});

test("S2: the taskkill of the stop is bounded by a 10 s timeout", async () => {
  const child = fakeChild();
  const calls = [];
  const spawnSyncImpl = (command, args, options) => {
    calls.push(options);
    markExited(child);
    return { status: 0 };
  };
  await stopServerProcess(child, { platform: "win32", graceMs: 1_000, killMs: 1_000, spawnSyncImpl });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].timeout, TASKKILL_TIMEOUT_MS);
  assert.equal(calls[0].stdio, "ignore");
});

test("POSIX: the process group is still sent SIGKILL after its leader has exited (a grandchild may hold the port); the Windows rule does not apply", async (t) => {
  const heldPort = await holdPort(t, answerOk);
  const child = fakeChild();
  const kills = [];
  const killImpl = (pid, signal) => {
    kills.push([pid, signal]);
    if (signal === "SIGTERM") markExited(child, 0);       // the leader dies of the SIGTERM; the port stays held
  };
  await assert.rejects(
    stopServerProcess(child, { port: heldPort, platform: "linux", graceMs: 150, killMs: 150, pollMs: 25, killImpl }),
    /could not stop/,
  );
  assert.deepEqual(kills, [[-UNUSED_PID, "SIGTERM"], [-UNUSED_PID, "SIGKILL"]]);
});

test("W2: the tests' own reap() runs no taskkill for an exited child on Windows, a bounded one for a live child, and still signals a POSIX group", () => {
  const calls = [];
  const spawnSyncImpl = (command, args, options) => { calls.push({ args, options }); };
  reap(fakeChild({ exited: true }), { platform: "win32", spawnSyncImpl });
  assert.equal(calls.length, 0, "no taskkill for a child that has exited");
  reap(fakeChild(), { platform: "win32", spawnSyncImpl });
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].args, ["/pid", String(UNUSED_PID), "/T", "/F"]);
  assert.equal(calls[0].options.timeout, TASKKILL_TIMEOUT_MS);
  const kills = [];
  reap(fakeChild({ exited: true }), { platform: "linux", killImpl: (pid, signal) => kills.push([pid, signal]) });
  assert.deepEqual(kills, [[-UNUSED_PID, "SIGKILL"]], "a POSIX group is signalled whether or not its leader has exited");
});

// ---- (e) W1 and S1: Ctrl-C or a CI cancel at any point of a run stops everything and exits with the signal's code
//
// installShutdown owns the end of a run: the SIGINT/SIGTERM/SIGHUP handlers (installed BEFORE the server is brought up, over a variable that
// start() sets), one memoized stop that the handler, the failed bring-up and the finally all share, an async run() for the Lighthouse
// child that the handler can kill, and unlessSignalled() so that the code after a killed step cannot report or exit by itself.
// The first group uses a fake process object (any platform). The second runs a stub program in a real child process with the real
// fixture_guard.mjs, real server children and a real stop; where a real signal cannot be sent (Windows) the test writes the signal's
// name to the stub's stdin and the stub emits it on its process object, which calls exactly the handlers a real signal calls.

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

// installShutdown over a fake process: the test emits the signals, exit() and log() are recorded
function shutdownHarness(options = {}) {
  const proc = new EventEmitter();
  const exit = deferred();
  const exits = [];
  const logs = [];
  const shutdown = installShutdown({
    stopOptions: STOP, proc, exit: (code) => { exits.push(code); exit.resolve(code); }, log: (line) => logs.push(line), ...options,
  });
  return { proc, shutdown, exits, logs, exited: exit.promise };
}

test("a signal stops the server and only then exits with the signal's code: 130 for SIGINT, 143 for SIGTERM", async () => {
  for (const [signal, code] of [["SIGINT", 130], ["SIGTERM", 143]]) {
    const gate = deferred();
    let stops = 0;
    const h = shutdownHarness({ stopServer: () => { stops += 1; return gate.promise; } });
    h.proc.emit(signal);
    await pause(30);
    assert.deepEqual(h.exits, [], `${signal}: no exit while the stop is still running`);
    gate.resolve();
    assert.equal(await h.exited, code, signal);
    assert.equal(stops, 1, signal);
  }
});

test("a second signal while the first is being handled changes nothing: one stop, one exit, the first signal's code", async () => {
  const gate = deferred();
  let stops = 0;
  const h = shutdownHarness({ stopServer: () => { stops += 1; return gate.promise; } });
  h.proc.emit("SIGINT");
  h.proc.emit("SIGTERM");
  h.proc.emit("SIGINT");
  gate.resolve();
  assert.equal(await h.exited, 130);
  await pause(50);
  assert.deepEqual(h.exits, [130]);
  assert.equal(stops, 1);
});

test("a stop that fails is reported and the exit code is still the signal's", async () => {
  const h = shutdownHarness({ stopServer: async () => { throw new Error("pid 4242 did not exit"); } });
  h.proc.emit("SIGTERM");
  assert.equal(await h.exited, 143);
  assert.ok(h.logs.some((line) => /pid 4242 did not exit/.test(line)), h.logs.join("\n"));
});

test("a signal before any server exists exits at once (nothing to stop yet)", async () => {
  const h = shutdownHarness();
  h.proc.emit("SIGTERM");
  assert.equal(await h.exited, 143);
});

test("received() is null until a signal arrives and then names the first one; uninstall() takes the handlers off the process", async () => {
  const h = shutdownHarness();
  assert.equal(h.shutdown.received(), null);
  assert.equal(h.proc.listenerCount("SIGINT"), 1);
  assert.equal(h.proc.listenerCount("SIGTERM"), 1);
  h.proc.emit("SIGINT");
  h.proc.emit("SIGTERM");
  assert.deepEqual(h.shutdown.received(), { signal: "SIGINT", code: 130 });
  await h.exited;
  h.shutdown.uninstall();
  assert.equal(h.proc.listenerCount("SIGINT"), 0);
  assert.equal(h.proc.listenerCount("SIGTERM"), 0);
});

// L1: on POSIX the fixture server and the Lighthouse child are detached into sessions of their own, so a closed terminal or a dropped
// ssh session sends its SIGHUP to node only. Without a handler node dies of it and both children go on running, orphaned.
const within = (promise, ms = 5_000) => Promise.race([promise, pause(ms).then(() => "timed out")]);

test("L1: SIGHUP takes the stop-then-exit path of SIGINT and SIGTERM: exit 129 after one stop, and run() starts nothing afterwards", async () => {
  const gate = deferred();
  let stops = 0;
  const h = shutdownHarness({ stopServer: () => { stops += 1; return gate.promise; } });
  assert.equal(h.proc.listenerCount("SIGHUP"), 1, "installShutdown must listen for SIGHUP");
  assert.equal(h.proc.listenerCount("SIGQUIT"), 0, "SIGQUIT stays unhandled: handling it would suppress the core dump of a Ctrl-backslash");
  h.proc.emit("SIGHUP");
  await pause(30);
  assert.deepEqual(h.shutdown.received(), { signal: "SIGHUP", code: 129 });
  assert.deepEqual(h.exits, [], "no exit while the stop is still running");
  assert.equal(stops, 1, "the handler started the stop");
  await assert.rejects(h.shutdown.run(NODE, ["-e", "0"]), /SIGHUP/);
  h.proc.emit("SIGTERM");                                  // a second signal while the first is being handled changes nothing
  gate.resolve();
  assert.equal(await within(h.exited), 129);
  assert.equal(stops, 1, "one stop, whoever asks for it");
  await h.shutdown.stop();
  assert.equal(stops, 1, "stop() after the signal is the same stop");
  await assert.rejects(h.shutdown.run(NODE, ["-e", "0"]), /SIGHUP/, "run() refuses for good once a signal has been received");
  assert.deepEqual(h.exits, [129]);
  h.shutdown.uninstall();
  assert.equal(h.proc.listenerCount("SIGHUP"), 0, "uninstall() takes the SIGHUP handler off too");
});

test("S1: stop() is one stop however often and from wherever it is called; the signal handler and the finally share it", async () => {
  const gate = deferred();
  let stops = 0;
  const h = shutdownHarness({ stopServer: () => { stops += 1; return gate.promise; } });
  const finallyStop = h.shutdown.stop();                  // the finally of lighthouse.mjs
  assert.equal(h.shutdown.stop(), finallyStop, "the same promise");
  h.proc.emit("SIGTERM");                                  // the handler, while that stop is still running
  gate.resolve();
  await finallyStop;
  assert.equal(await h.exited, 143);
  assert.equal(stops, 1, "one stop, not two running at once");
});

test("S1: a signal during the final stop leaves one outcome: exit 143 and no success message", async () => {
  const gate = deferred();
  const reported = [];
  const h = shutdownHarness({ stopServer: () => gate.promise });
  const mainFlow = (async () => {                          // the end of lighthouse.mjs
    await h.shutdown.stop();
    await h.shutdown.unlessSignalled();
    reported.push("Lighthouse gates passed.");
  })();
  h.proc.emit("SIGTERM");
  gate.resolve();
  assert.equal(await h.exited, 143);
  await pause(50);
  assert.deepEqual(reported, [], "the exit code 143 and a success message must not both be produced");
  assert.deepEqual(h.exits, [143]);
  assert.equal(await Promise.race([mainFlow.then(() => "returned"), pause(20).then(() => "parked")]), "parked");
});

test("S1: without a signal the main flow goes on after the stop and nothing has exited", async () => {
  const h = shutdownHarness();
  await h.shutdown.stop();
  await h.shutdown.unlessSignalled();
  assert.deepEqual(h.exits, []);
});

test("run() resolves with the status and the output of the child", async () => {
  const h = shutdownHarness();
  const result = await h.shutdown.run(NODE, ["-e", 'console.log("to out"); console.error("to err"); process.exitCode = 3;']);
  assert.equal(result.status, 3);
  assert.equal(result.stdout.trim(), "to out");
  assert.equal(result.stderr.trim(), "to err");
});

test("run() rejects when the program cannot be started", async () => {
  const h = shutdownHarness();
  await assert.rejects(h.shutdown.run("definitely-not-a-program-xyz-0451", []), /ENOENT/);
});

test("run() does not block the event loop: timers fire while the child runs (a spawnSync would stall them)", async () => {
  const h = shutdownHarness();
  let ticks = 0;
  const timer = setInterval(() => { ticks += 1; }, 20);
  try {
    await h.shutdown.run(NODE, ["-e", "setTimeout(() => {}, 500)"]);
  } finally {
    clearInterval(timer);
  }
  assert.ok(ticks >= 5, `only ${ticks} timer ticks during a 500 ms child`);
});

test("a signal stops the running child (the Lighthouse run) too, and the exit comes after the child is gone", async (t) => {
  const h = shutdownHarness();
  const running = h.shutdown.run(NODE, ["-e", `${SELF_DESTRUCT} setInterval(() => {}, 1000);`]);
  const child = running.child;
  t.after(() => reap(child));
  h.proc.emit("SIGTERM");
  assert.equal(await Promise.race([h.exited, pause(10_000).then(() => "timed out")]), 143, "the exit must follow the stop, which must kill the child");
  assertExited(child);                       // already gone when exit() was called, not merely soon
  await running;
});

test("after a signal run() starts nothing", async () => {
  const h = shutdownHarness();
  h.proc.emit("SIGTERM");
  await h.exited;
  await assert.rejects(h.shutdown.run(NODE, ["-e", "0"]), /SIGTERM/);
});

test("W1: lighthouse.mjs installs the shutdown before it brings the server up and runs Lighthouse through it, without spawnSync", () => {
  const source = readFileSync(LIGHTHOUSE_SOURCE, "utf8");
  const install = source.search(/=\s*installShutdown\(/);
  const bringUp = source.indexOf("await bringUpServer(");
  assert.ok(install >= 0, "lighthouse.mjs must call installShutdown");
  assert.ok(bringUp >= 0, "lighthouse.mjs must await bringUpServer");
  assert.ok(install < bringUp, "the signal handlers must exist before the readiness wait of up to 180 s begins");
  assert.match(source, /shutdown\.run\(/, "Lighthouse must run through the shutdown's async, killable run()");
  assert.match(source, /stop:\s*shutdown\.stop\b/, "the failed bring-up must share the one memoized stop");
  assert.match(source, /unlessSignalled\(\)/, "after a signal the main flow must not report or exit by itself");
  assert.doesNotMatch(source, /spawnSync/, "a synchronous child blocks every signal handler until the run ends");
});

// The stub: lighthouse.mjs in miniature, run as a child process. argv: mode, port, the source of the server, the source of the run.
//   bringup  the server never answers 2xx, so the run stays in the readiness wait
//   run      the server answers; then one child (the Lighthouse stand-in) runs through shutdown.run
//   stop     the server answers; the stop in the finally takes 1.5 s, so a signal can arrive during it
const STUB_SOURCE = `
import { spawn } from "node:child_process";
import { writeSync } from "node:fs";
import * as guard from __GUARD_URL__;

const [mode, portText, serverSource, runSource] = process.argv.slice(2);
const port = Number(portText);
// Synchronous writes: a line written just before process.exit is never lost, so the test sees exactly what the run printed.
const say = (line) => writeSync(1, line + "\\n");
const finish = (line, code) => {
  say(line);
  process.exit(code);
};
const gone = (child) => !child || child.exitCode !== null || child.signalCode !== null;
let server = null;
let runChild = null;
let stops = 0;

const shutdown = guard.installShutdown({
  stopOptions: { graceMs: 5000, killMs: 5000 },
  // what the run would exit with, and what is still alive at that moment: the exit must come after the stop, not before it
  exit: (code) => finish("EXITING " + code + " server=" + (gone(server) ? "gone" : "alive") + " run=" + (gone(runChild) ? "gone" : "alive"), code),
  stopServer: async () => {
    if (!server) return;
    stops += 1;
    say("STOP " + stops);
    if (mode === "stop") await new Promise((resolve) => setTimeout(resolve, 1500));
    await guard.stopServerProcess(server, { port, graceMs: 5000, killMs: 5000 });
  },
});
process.stdin.on("data", (data) => {
  const name = String(data).trim();
  say("SIMULATED " + name);
  process.emit(name);
});

try {
  await guard.bringUpServer({
    port, readyMs: 60000, intervalMs: 25, stop: shutdown.stop,
    start: () => {
      server = spawn(process.execPath, ["-e", serverSource], { stdio: "ignore", ...guard.groupLeaderOptions() });
      say("STARTED " + server.pid);
      return server;
    },
  });
  say("UP");
} catch (err) {
  await shutdown.unlessSignalled();
  await finish("FAILED " + err.message, 1);
}

let failure = null;
try {
  if (mode === "run") {
    const running = shutdown.run(process.execPath, ["-e", runSource]);
    runChild = running.child;
    say("RUNNING " + running.child.pid);
    const result = await running;
    if (result.status !== 0) throw new Error("the run ended with status " + result.status);
  }
} catch (err) {
  await shutdown.unlessSignalled();
  failure = err;
} finally {
  try {
    await shutdown.stop();
  } catch (err) {
    say("STOPFAILED " + err.message);
  }
}
await shutdown.unlessSignalled();
await finish(failure ? "FINAL failed: " + failure.message : "FINAL passed", failure ? 1 : 0);
`;

function runStub(t, { mode, port, serverSource, runSource = "0" }) {
  const dir = mkdtempSync(path.join(tmpdir(), "lh-stub-"));
  const file = path.join(dir, "stub.mjs");
  writeFileSync(file, STUB_SOURCE.replace("__GUARD_URL__", () => JSON.stringify(GUARD_URL)));
  const child = spawn(NODE, [file, mode, String(port), serverSource, runSource], { stdio: ["pipe", "pipe", "pipe"] });
  child.on("error", () => {});
  let stdout = "";
  let stderr = "";
  child.stdout.setEncoding("utf8").on("data", (chunk) => { stdout += chunk; });
  child.stderr.setEncoding("utf8").on("data", (chunk) => { stderr += chunk; });
  const done = new Promise((resolve) => child.once("close", (code, signal) => resolve({ code, signal })));
  t.after(() => {
    reap(child);
    rmSync(dir, { recursive: true, force: true, maxRetries: 5 });
  });
  const lines = () => stdout.split(/\r?\n/).filter(Boolean);
  async function waitFor(pattern, ms = 20_000) {
    const deadline = Date.now() + ms;
    for (;;) {
      const line = lines().find((candidate) => pattern.test(candidate));
      if (line) return line.match(pattern);
      if (child.exitCode !== null || child.signalCode !== null) throw new Error(`the stub exited before ${pattern}\n${stdout}${stderr}`);
      if (Date.now() > deadline) throw new Error(`no ${pattern} from the stub within ${ms} ms\n${stdout}${stderr}`);
      await pause(20);
    }
  }
  return { child, done, lines, stderr: () => stderr, waitFor, simulate: (name) => child.stdin.write(`${name}\n`) };
}

// There is deliberately no "is this pid still alive" check after the stub has exited: Windows hands a pid to an unrelated process
// quickly, so process.kill(pid, 0) could then succeed for a process that is not ours. The evidence that the server and the run are
// gone is the stub's own EXITING line (written synchronously from its live child handles just before it exits) and the port.

// resolves once something accepts connections on `port`
async function listening(port, ms = 15_000) {
  const deadline = Date.now() + ms;
  for (;;) {
    const accepted = await new Promise((resolve) => {
      const socket = net.connect({ port, host: "127.0.0.1" });
      socket.once("connect", () => { socket.destroy(); resolve(true); });
      socket.once("error", () => resolve(false));
    });
    if (accepted) return;
    if (Date.now() > deadline) throw new Error(`nothing listens on ${port}`);
    await pause(25);
  }
}

const LONG_RUN = `${SELF_DESTRUCT} setInterval(() => {}, 1000);`;

test("W1: a signal during the readiness wait stops the half-started server and exits with the signal's code", async (t) => {
  const port = await freePort();
  const stub = runStub(t, { mode: "bringup", port, serverSource: slowCloseSource(port, 503) });
  await stub.waitFor(/^STARTED (\d+)$/);
  await listening(port);                                   // up, its SIGTERM handling installed, answering 503: never ready
  stub.simulate("SIGTERM");
  assert.deepEqual(await stub.done, { code: 143, signal: null });
  assert.equal(stub.lines().some((line) => line.startsWith("FAILED")), false, "the failed bring-up must not report or exit by itself");
  assert.ok(stub.lines().includes("EXITING 143 server=gone run=gone"), stub.lines().join("\n"));
  await assertPortFree(port);
});

test("W1: a signal during a Lighthouse run kills the run and the server, exits with the signal's code and reports no success", async (t) => {
  const port = await freePort();
  const stub = runStub(t, { mode: "run", port, serverSource: slowCloseSource(port, 200), runSource: LONG_RUN });
  await stub.waitFor(/^STARTED (\d+)$/);
  await stub.waitFor(/^RUNNING (\d+)$/);
  stub.simulate("SIGINT");
  assert.deepEqual(await stub.done, { code: 130, signal: null });
  assert.equal(stub.lines().some((line) => line.startsWith("FINAL")), false, "no final message after a signal");
  assert.ok(stub.lines().includes("EXITING 130 server=gone run=gone"), "the run and the server must be gone when the exit is called\n" + stub.lines().join("\n"));
  await assertPortFree(port);
});

test("L1: a SIGHUP during a Lighthouse run kills the run and the server, exits 129 and reports no success", async (t) => {
  const port = await freePort();
  const stub = runStub(t, { mode: "run", port, serverSource: slowCloseSource(port, 200), runSource: LONG_RUN });
  await stub.waitFor(/^STARTED (\d+)$/);
  await stub.waitFor(/^RUNNING (\d+)$/);
  stub.simulate("SIGHUP");
  assert.deepEqual(await within(stub.done, 15_000), { code: 129, signal: null });
  const lines = stub.lines();
  assert.equal(lines.some((line) => line.startsWith("FINAL")), false, "no final message after a signal");
  assert.deepEqual(lines.filter((line) => line.startsWith("STOP")), ["STOP 1"], "one stop");
  assert.ok(lines.includes("EXITING 129 server=gone run=gone"), "the run and the server must be gone when the exit is called\n" + lines.join("\n"));
  await assertPortFree(port);
});

test("S1: a signal during the final stop shares that stop: one stop, exit 143 and no success message", async (t) => {
  const port = await freePort();
  const stub = runStub(t, { mode: "stop", port, serverSource: slowCloseSource(port, 200) });
  await stub.waitFor(/^STARTED (\d+)$/);
  await stub.waitFor(/^STOP 1$/);                          // the finally's stop has begun and takes 1.5 s
  stub.simulate("SIGTERM");
  assert.deepEqual(await stub.done, { code: 143, signal: null });
  const lines = stub.lines();
  assert.deepEqual(lines.filter((line) => line.startsWith("STOP")), ["STOP 1"], "one stop, not two");
  assert.equal(lines.some((line) => line.startsWith("FINAL")), false, "exit code 143 and a final message must agree");
  assert.ok(lines.includes("EXITING 143 server=gone run=gone"), lines.join("\n"));
  await assertPortFree(port);
});

test("without a signal the run ends as before: success reported, exit 0, one stop, the server gone", async (t) => {
  const port = await freePort();
  const stub = runStub(t, { mode: "run", port, serverSource: slowCloseSource(port, 200), runSource: "0" });
  await stub.waitFor(/^STARTED (\d+)$/);
  assert.deepEqual(await stub.done, { code: 0, signal: null });
  const lines = stub.lines();
  assert.ok(lines.includes("FINAL passed"), lines.join("\n"));
  assert.deepEqual(lines.filter((line) => line.startsWith("STOP")), ["STOP 1"]);
  await assertPortFree(port);
});

test("POSIX: a real SIGTERM during the readiness wait stops the server and exits 143 (not killed by the signal)", { skip: IS_WINDOWS && "a real SIGTERM ends a Windows process without running handlers; the simulated signal above covers the logic" }, async (t) => {
  const port = await freePort();
  const stub = runStub(t, { mode: "bringup", port, serverSource: slowCloseSource(port, 503) });
  await stub.waitFor(/^STARTED (\d+)$/);
  await listening(port);
  stub.child.kill("SIGTERM");
  assert.deepEqual(await stub.done, { code: 143, signal: null });
  assert.ok(stub.lines().includes("EXITING 143 server=gone run=gone"), "the server (a process group of its own) must be gone\n" + stub.lines().join("\n"));
  await assertPortFree(port);
});

test("POSIX: a real SIGINT during a Lighthouse run kills the run and the server and exits 130", { skip: IS_WINDOWS && "a real SIGINT is not deliverable to a Windows process by pid; the simulated signal above covers the logic" }, async (t) => {
  const port = await freePort();
  const stub = runStub(t, { mode: "run", port, serverSource: slowCloseSource(port, 200), runSource: LONG_RUN });
  await stub.waitFor(/^STARTED (\d+)$/);
  await stub.waitFor(/^RUNNING (\d+)$/);
  await listening(port);
  stub.child.kill("SIGINT");
  assert.deepEqual(await stub.done, { code: 130, signal: null });
  assert.ok(stub.lines().includes("EXITING 130 server=gone run=gone"), "the run and the server must be gone\n" + stub.lines().join("\n"));
  await assertPortFree(port);
});

// L1 on POSIX with the real signal: a closed terminal sends SIGHUP to node only (the server and the run child lead sessions of their own).
test("POSIX: a real SIGHUP during a Lighthouse run kills the run and the server and exits 129", { skip: IS_WINDOWS && "a real SIGHUP is not deliverable to a Windows process; the simulated SIGHUP above covers the logic" }, async (t) => {
  const port = await freePort();
  const stub = runStub(t, { mode: "run", port, serverSource: slowCloseSource(port, 200), runSource: LONG_RUN });
  await stub.waitFor(/^STARTED (\d+)$/);
  await stub.waitFor(/^RUNNING (\d+)$/);
  await listening(port);
  stub.child.kill("SIGHUP");
  assert.deepEqual(await within(stub.done, 15_000), { code: 129, signal: null });
  const lines = stub.lines();
  assert.equal(lines.some((line) => line.startsWith("FINAL")), false, "no final message after a signal");
  assert.ok(lines.includes("EXITING 129 server=gone run=gone"), "the run and the server (sessions of their own) must be gone\n" + lines.join("\n"));
  await assertPortFree(port);
});
