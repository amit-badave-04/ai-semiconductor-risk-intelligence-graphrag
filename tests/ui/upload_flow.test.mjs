// Behavioural tests for two round-2-review findings that the small pure-function harness (harness.mjs's `loadPage`)
// cannot reach, because `setWorkspace`/`renderDocuments`/`uploadFile`/`handleFiles`/`watchJob`/
// `waitForFreshTurnstileToken` are stateful and close over page-global `let`s (`currentWorkspace`,
// `wsAskAsOfInstant`, `turnstileToken`) rather than being pure. This file therefore runs the page's REAL inline
// script in its own `vm` context (like the round-2 reviewers' own throwaway repros did), with fakes for `fetch`,
// `FormData` and the DOM, and drives it exactly the way a browser would.
//
//   - C3: the "ask as of vN" instant must reset on a workspace switch and be correctly re-applied (or dropped)
//     after every render of the document list, and at most one document's selector may show a non-"current" value.
//   - R3 / finding 15 (multi-file drop): the next file must not be POSTed until the previous file's job reaches a
//     terminal state (it holds the single upload slot), and never with a stale/empty Turnstile token.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { INDEX_PATH } from "./harness.mjs";

function rawScript(siteKey = "") {
  const html = readFileSync(INDEX_PATH, "utf8");
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  if (scripts.length !== 1) throw new Error(`expected exactly one inline <script>, found ${scripts.length}`);
  return scripts[0][1].replace("__TURNSTILE_SITE_KEY__", siteKey);
}

// A generic stub for any element reached only through `$(id)` (never through `innerHTML`-driven traversal).
function stub() {
  const kids = {};
  return {
    style: {}, dataset: {}, innerHTML: "", textContent: "", value: "", hidden: false, disabled: false, className: "",
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, appendChild(c) { return c; }, setAttribute() {}, remove() {},
    querySelectorAll() { return []; }, closest() { return null; },
    querySelector(sel) { return (kids[sel] ||= stub()); },
  };
}

// A fake `<select class="askAsOf">`: `options` is the plain `{value}` list `versionAskAsOfOptions` would have
// produced; `fireChange(v)` simulates the user picking one, running the SAME listener `renderDocuments` attaches.
function selectStub(optionValues) {
  const el = {
    dataset: {}, className: "askAsOf", _value: "", _onChange: null,
    get value() { return el._value; }, set value(v) { el._value = v; },
    options: optionValues.map((value) => ({ value })),
    addEventListener(evt, fn) { if (evt === "change") el._onChange = fn; },
  };
  el.fireChange = (v) => { el._value = v; if (el._onChange) el._onChange(); };
  return el;
}

// Builds one context. `wsDocsSelects()` lets a test control exactly what `renderDocuments` finds when it queries
// `#wsDocs` for `select.askAsOf` — decoupled from the innerHTML string `docHtml` actually produces, the same way
// tests/ui/agent.test.mjs hand-builds the `<option>` elements `syncAgentUi` looks for.
function buildPage({ siteKey = "", fetchImpl, formDataImpl, wsDocsSelects = () => [] } = {}) {
  const elements = new Map();
  const rows = {};
  const wsDocs = { ...stub(), querySelectorAll: (sel) => (sel === "select.askAsOf" ? wsDocsSelects() : []) };
  elements.set("wsDocs", wsDocs);
  const document_ = {
    getElementById(id) {
      if (id.startsWith("wsJobRow-")) return rows[id] || null;
      if (!elements.has(id)) elements.set(id, stub());
      return elements.get(id);
    },
    addEventListener() {},
    createElement(tag) {
      const e = { tag, ...stub() };
      return new Proxy(e, { set(t, k, v) { t[k] = v; if (k === "id") rows[v] = t; return true; } });
    },
    head: { appendChild() {} },
  };
  const sandbox = {
    document: document_, window: { turnstile: null }, console, TextDecoder, setTimeout,
    fetch: fetchImpl || (() => new Promise(() => {})),
    FormData: formDataImpl || class { append() {} },
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(rawScript(siteKey), ctx);
  const run = (src) => vm.runInContext(src, ctx);
  return { ctx, run, elements, rows, wsDocs };
}

// ---------------------------------------------------------------- C3: ask-as-of survives re-render / workspace switch

test("renderDocuments re-applies the stored ask-as-of instant to whichever select still offers it", () => {
  const selA = selectStub(["", "2026-01-01T00:00:00Z"]);
  const selB = selectStub(["", "2026-02-01T00:00:00Z"]);
  const { run } = buildPage({ wsDocsSelects: () => [selA, selB] });
  run(`wsAskAsOfInstant = "2026-02-01T00:00:00Z";`);
  run(`renderDocuments([{ document_id: "a", versions: [] }, { document_id: "b", versions: [] }]);`);
  assert.equal(selB.value, "2026-02-01T00:00:00Z");
  assert.equal(selA.value, "");   // at most one selector is ever left non-"current"
  assert.equal(run(`wsAskAsOfInstant`), "2026-02-01T00:00:00Z");
});

test("renderDocuments nulls the stored instant once no select offers it any more (its document/version is gone)", () => {
  const selA = selectStub(["", "2026-01-01T00:00:00Z"]);
  const { run } = buildPage({ wsDocsSelects: () => [selA] });
  run(`wsAskAsOfInstant = "2026-09-01T00:00:00Z";`);   // stale: no longer any option with this value
  run(`renderDocuments([{ document_id: "a", versions: [] }]);`);
  assert.equal(selA.value, "");
  assert.equal(run(`wsAskAsOfInstant`), null);
  assert.equal(run(`wsAsOfValue()`), null);   // a later ask() falls back to the manual date picker, not a dead instant
});

test("picking a version on one select clears every other document's select (at most one active choice)", () => {
  const selA = selectStub(["", "2026-01-01T00:00:00Z"]);
  const selB = selectStub(["", "2026-02-01T00:00:00Z"]);
  selB.value = "2026-02-01T00:00:00Z";   // simulates a PRIOR render having applied B's own earlier choice
  const { run } = buildPage({ wsDocsSelects: () => [selA, selB] });
  run(`renderDocuments([{ document_id: "a", versions: [] }, { document_id: "b", versions: [] }]);`);
  selA.fireChange("2026-01-01T00:00:00Z");
  assert.equal(run(`wsAskAsOfInstant`), "2026-01-01T00:00:00Z");
  assert.equal(selB.value, "");   // B's stale selection is cleared the moment A's is chosen
});

test("setWorkspace resets the ask-as-of instant, so switching (or deleting/creating) a workspace never reuses it", () => {
  const { run } = buildPage({ fetchImpl: () => new Promise(() => {}) });   // loadWorkspaceData's fetch never resolves
  run(`wsAskAsOfInstant = "2026-01-01T00:00:00Z";`);
  run(`setWorkspace(null);`);   // deleteWorkspace()/createWorkspace()/restoreWorkspace() all funnel through this
  assert.equal(run(`wsAskAsOfInstant`), null);
  assert.equal(run(`wsAsOfValue()`), null);
});

// ---------------------------------------------------------------- R3 / finding 15: multi-file drop sequencing

// Models the server's single `upload_slots` permit: a POST while `busy` is true answers 429 "busy"; otherwise it
// answers 202 and the slot stays held until the SAME job's SSE stream reports its terminal state, exactly as the
// real job thread (not the route) releases it.
function slotModelFetch({ jobDelayMs = 15 } = {}) {
  let busy = false;
  const posts = [];
  let n = 0;
  const fetchImpl = async (url, opts = {}) => {
    if (url.includes("/documents") && opts.method === "POST") {
      posts.push({ url, token: (opts.headers || {})["X-Turnstile-Token"] });
      if (busy) return { ok: false, status: 429, json: async () => ({ detail: "busy", code: "busy" }) };
      busy = true;
      n += 1;
      return { ok: true, status: 202, json: async () => ({ job_id: `job${n}`, document_id: "doc", version: 1 }) };
    }
    if (url.includes("/jobs/")) {
      let sent = false;
      return {
        ok: true,
        body: { getReader: () => ({
          read: () => new Promise((resolve) => setTimeout(() => {
            if (sent) { resolve({ done: true }); return; }
            sent = true;
            busy = false;   // the WORKER thread frees the slot exactly when it reports the terminal event
            const chunk = 'event: job\ndata: {"state":"ready","version":1}\n\n';
            resolve({ done: false, value: new TextEncoder().encode(chunk) });
          }, sent ? 0 : jobDelayMs)),
        }) },
      };
    }
    return new Promise(() => {});
  };
  return { fetchImpl, posts };
}

test("handleFiles waits for the previous file's job to finish before posting the next one", async () => {
  const { fetchImpl, posts } = slotModelFetch({ jobDelayMs: 15 });
  const { run } = buildPage({ fetchImpl });
  run(`currentWorkspace = { id: "${"a".repeat(32)}", token: "t" };`);
  await run(`handleFiles([{ name: "a.pdf" }, { name: "b.pdf" }])`);
  assert.equal(posts.length, 2);
  // Before the fix, the second POST fired immediately (fire-and-forget watchJob) and got 429 busy. Proving BOTH
  // files ended up on the workspace (neither is a "busy" bounce) is exactly the behaviour finding 15/R3 asks for.
  assert.equal(posts[0].url, `/api/workspace/${"a".repeat(32)}/documents`);
  assert.equal(posts[1].url, `/api/workspace/${"a".repeat(32)}/documents`);
});

test("each dropped file's own outcome is shown, not just the last one's", async () => {
  const { fetchImpl } = slotModelFetch({ jobDelayMs: 5 });
  const { run, rows } = buildPage({ fetchImpl });
  run(`currentWorkspace = { id: "${"a".repeat(32)}", token: "t" };`);
  await run(`handleFiles([{ name: "a.pdf" }, { name: "b.pdf" }])`);
  assert.equal(rows["wsJobRow-a.pdf"].querySelector(".state").textContent, "ready");
  assert.equal(rows["wsJobRow-b.pdf"].querySelector(".state").textContent, "ready");
});

// ---------------------------------------------------------------- R3 / finding 15: never send a stale/empty token

test("waitForFreshTurnstileToken resolves immediately when Turnstile is not embedded on the page", async () => {
  const { run } = buildPage({ siteKey: "" });
  assert.equal(await run(`waitForFreshTurnstileToken(50)`), null);
});

test("waitForFreshTurnstileToken resolves immediately with the current token when one is already set", async () => {
  const { run } = buildPage({ siteKey: "site-key" });
  run(`turnstileToken = "already-have-one";`);
  assert.equal(await run(`waitForFreshTurnstileToken(50)`), "already-have-one");
});

test("waitForFreshTurnstileToken resolves to a fresh token once the widget's callback sets one", async () => {
  const { run } = buildPage({ siteKey: "site-key" });
  run(`turnstileToken = null;`);
  setTimeout(() => run(`turnstileToken = "fresh-token";`), 20);
  assert.equal(await run(`waitForFreshTurnstileToken(500)`), "fresh-token");
});

test("waitForFreshTurnstileToken gives up (undefined) rather than wait forever for a widget that never re-solves", async () => {
  const { run } = buildPage({ siteKey: "site-key" });
  run(`turnstileToken = null;`);
  assert.equal(await run(`waitForFreshTurnstileToken(30)`), undefined);
});

test("a second dropped file waits out a slow Turnstile re-solve instead of sending an empty token", async () => {
  const { fetchImpl, posts } = slotModelFetch({ jobDelayMs: 5 });
  const { run } = buildPage({ siteKey: "site-key", fetchImpl });
  run(`currentWorkspace = { id: "${"a".repeat(32)}", token: "t" };`);
  run(`turnstileToken = "initial-token";`);
  // uploadFile() calls resetTurnstile() after EVERY post; make that null the token and — like the real widget after
  // reset() — re-solve a moment later, well inside the wait window.
  run(`window.turnstile = { reset() { turnstileToken = null; setTimeout(() => { turnstileToken = "refreshed-token"; }, 5); } };`);
  await run(`handleFiles([{ name: "a.pdf" }, { name: "b.pdf" }])`);
  assert.equal(posts.length, 2);
  assert.equal(posts[0].token, "initial-token");
  assert.equal(posts[1].token, "refreshed-token");   // never "" — the stale/empty token finding 15/R3 flagged
});

// ---------------------------------------------------------------- a dropped progress stream reconnects (finding 27, UI half)

// Only the job-progress URL is scripted; the page's own startup requests (examples, stats, freshness) never resolve.
const jobsOnly = (answer) => (url, opts) => (String(url).includes("/jobs/") ? answer(url, opts) : new Promise(() => {}));

function streamOf(events) {
  let i = 0;
  return { ok: true, status: 200, body: { getReader: () => ({
    read: async () => (i < events.length
      ? { done: false, value: new TextEncoder().encode(`event: job\ndata: ${JSON.stringify(events[i++])}\n\n`) }
      : { done: true }),
  }) } };
}

test("a progress stream that ends without a terminal event is reconnected, and the replayed final state is shown", async () => {
  const answers = [streamOf([{ state: "embedding", progress: { done: 3, total: 40 } }]),
                   { ok: false, status: 429, body: null },          // the watcher cap: retry, never give up on it
                   streamOf([{ state: "ready", version: 1 }])];
  let calls = 0;
  const { run, rows } = buildPage({ fetchImpl: jobsOnly(async () => answers[calls++]) });
  run(`currentWorkspace = { id: "${"a".repeat(32)}", token: "t" }; setJobRow("a.pdf", "uploading");`);
  assert.equal(await run(`watchJob("j1", "a.pdf", 1)`), "ready");
  assert.equal(calls, 3);
  assert.equal(rows["wsJobRow-a.pdf"].querySelector(".state").textContent, "ready");
});

test("a 5xx while watching (what a restart or deploy returns) is retried, not treated as the job being gone", async () => {
  const answers = [{ ok: false, status: 503, body: null }, { ok: false, status: 502, body: null },
                   streamOf([{ state: "ready", version: 1 }])];
  let calls = 0;
  const { run, rows } = buildPage({ fetchImpl: jobsOnly(async () => answers[calls++]) });
  run(`currentWorkspace = { id: "${"a".repeat(32)}", token: "t" }; setJobRow("a.pdf", "uploading");`);
  assert.equal(await run(`watchJob("j1", "a.pdf", 1)`), "ready");
  assert.equal(calls, 3);
  assert.equal(rows["wsJobRow-a.pdf"].querySelector(".state").textContent, "ready");
});

test("after the reconnects run out the row says the connection was lost, and a 404 stops at once", async () => {
  let calls = 0;
  const lost = buildPage({ fetchImpl: jobsOnly(async () => { calls += 1; return streamOf([]); }) });
  lost.run(`currentWorkspace = { id: "${"a".repeat(32)}", token: "t" }; setJobRow("a.pdf", "uploading");`);
  assert.equal(await lost.run(`watchJob("j1", "a.pdf", 1)`), null);
  assert.equal(calls, 1 + lost.run(`WATCH_RECONNECTS`));
  assert.match(lost.rows["wsJobRow-a.pdf"].querySelector(".state").textContent, /connection lost/);
  let gone = 0;
  const deleted = buildPage({ fetchImpl: jobsOnly(async () => { gone += 1; return { ok: false, status: 404, body: null }; }) });
  deleted.run(`currentWorkspace = { id: "${"a".repeat(32)}", token: "t" }; setJobRow("a.pdf", "uploading");`);
  assert.equal(await deleted.run(`watchJob("j1", "a.pdf", 1)`), null);
  assert.equal(gone, 1);
});

// ---------------------------------------------------------------- FIX 1 (owner's live G10 test, 2026-09-30): the
// job watcher must drop stale `doc:` evidence the moment a job reaches a terminal state — a new version changes the
// status of the PREVIOUS version's chunks, so a chip opened before the upload must not keep showing that old payload.

test("reaching 'ready' drops cached doc: evidence but keeps a cached non-doc entry", async () => {
  const { run } = buildPage({ fetchImpl: jobsOnly(async () => streamOf([{ state: "ready", version: 2 }])) });
  run(`currentWorkspace = { id: "${"a".repeat(32)}", token: "t" }; setJobRow("a.pdf", "uploading");`);
  run(`evidenceCache.set("doc:0123456789ab:v1:0007", { title: "old" });`);
  run(`evidenceCache.set("xbrl:1045810:revenue:2026-01-25", { value: 1 });`);
  assert.equal(await run(`watchJob("j1", "a.pdf", 1)`), "ready");
  assert.equal(run(`evidenceCache.has("doc:0123456789ab:v1:0007")`), false);
  assert.equal(run(`evidenceCache.has("xbrl:1045810:revenue:2026-01-25")`), true);
});

test("reaching 'failed' also drops cached doc: evidence", async () => {
  const { run } = buildPage({ fetchImpl: jobsOnly(async () => streamOf([{ state: "failed", error: "bad file" }])) });
  run(`currentWorkspace = { id: "${"a".repeat(32)}", token: "t" }; setJobRow("a.pdf", "uploading");`);
  run(`evidenceCache.set("doc:0123456789ab:v1:0007", { title: "old" });`);
  assert.equal(await run(`watchJob("j1", "a.pdf", 1)`), "failed");
  assert.equal(run(`evidenceCache.has("doc:0123456789ab:v1:0007")`), false);
});

test("a non-terminal progress event leaves the evidence cache untouched", async () => {
  const { run } = buildPage({ fetchImpl: jobsOnly(async () => streamOf([{ state: "embedding", progress: { done: 1, total: 10 } }])) });
  run(`currentWorkspace = { id: "${"a".repeat(32)}", token: "t" }; setJobRow("a.pdf", "uploading");`);
  run(`evidenceCache.set("doc:0123456789ab:v1:0007", { title: "old" });`);
  await run(`streamJobOnce("j1", "a.pdf")`);
  assert.equal(run(`evidenceCache.has("doc:0123456789ab:v1:0007")`), true);
});

// Round-7 verification LOWs: a watcher that gives up, and an evidence fetch still in flight when the job finishes.

test("a watcher that gives up (connection lost) still drops cached doc: evidence: the job may finish anyway", async () => {
  const { run } = buildPage({ fetchImpl: jobsOnly(async () => streamOf([])) });
  run(`currentWorkspace = { id: "${"a".repeat(32)}", token: "t" }; setJobRow("a.pdf", "uploading");`);
  run(`evidenceCache.set("doc:0123456789ab:v1:0007", { title: "old" });`);
  assert.equal(await run(`watchJob("j1", "a.pdf", 1)`), null);
  assert.equal(run(`evidenceCache.has("doc:0123456789ab:v1:0007")`), false);
});

test("a doc: evidence fetch still in flight when the cache is dropped is shown but never cached", async () => {
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  const fetchImpl = (url) => (String(url).includes("/evidence/") ? pending : new Promise(() => {}));
  const { run } = buildPage({ fetchImpl });
  run(`currentWorkspace = { id: "${"a".repeat(32)}", token: "t" };`);
  const opened = run(`openCitation("doc:0123456789ab:v1:0007")`);
  run(`dropDocEvidence(evidenceCache);`);   // the upload job reached a terminal state meanwhile
  release({ ok: true, status: 200, json: async () => ({ id: "doc:0123456789ab:v1:0007", status: "current" }) });
  await opened;
  assert.equal(run(`evidenceCache.has("doc:0123456789ab:v1:0007")`), false);
});

test("a doc: evidence fetch still in flight across a workspace switch is never cached into the new workspace", async () => {
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  const fetchImpl = (url) => (String(url).includes("/evidence/") ? pending : new Promise(() => {}));
  const { run } = buildPage({ fetchImpl });
  run(`currentWorkspace = { id: "${"a".repeat(32)}", token: "t" };`);
  const opened = run(`openCitation("doc:0123456789ab:v1:0007")`);
  run(`setWorkspace(null);`);
  release({ ok: true, status: 200, json: async () => ({ id: "doc:0123456789ab:v1:0007", status: "current" }) });
  await opened;
  assert.equal(run(`evidenceCache.has("doc:0123456789ab:v1:0007")`), false);
});

test("an evidence fetch that finishes with no drop in between is cached as before", async () => {
  const fetchImpl = (url) => (String(url).includes("/evidence/")
    ? Promise.resolve({ ok: true, status: 200, json: async () => ({ id: "doc:0123456789ab:v1:0007" }) })
    : new Promise(() => {}));
  const { run } = buildPage({ fetchImpl });
  run(`currentWorkspace = { id: "${"a".repeat(32)}", token: "t" };`);
  await run(`openCitation("doc:0123456789ab:v1:0007")`);
  assert.equal(run(`evidenceCache.has("doc:0123456789ab:v1:0007")`), true);
});

test("deleting/switching the workspace (setWorkspace) still clears the WHOLE evidence cache, doc: and non-doc alike", async () => {
  const { run } = buildPage({ fetchImpl: () => new Promise(() => {}) });   // loadWorkspaceData's fetch never resolves
  run(`evidenceCache.set("doc:0123456789ab:v1:0007", { title: "old" });`);
  run(`evidenceCache.set("xbrl:1045810:revenue:2026-01-25", { value: 1 });`);
  await run(`setWorkspace(null);`);   // deleteWorkspace()/createWorkspace()/restoreWorkspace() all funnel through this
  assert.equal(run(`evidenceCache.size`), 0);
});
