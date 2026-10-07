// A cut answer stream (M5a closing review, V3). The server can end an answer mid-flight: a drain at shutdown that hits its
// timeout, a second signal, a dropped connection. The page used to have no try/catch around `reader.read()`, so a cut
// left the Ask button disabled and the status on "... generating…" for good. It must end the answer honestly instead:
// say the connection was lost, enable Ask again, and never overwrite how the server itself ended the answer.
import assert from "node:assert/strict";
import test from "node:test";
import { loadPage } from "./harness.mjs";

const encoder = new TextEncoder();
const sse = (event, data) => encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
const RETRIEVAL = sse("retrieval", { anchors: { Nvidia: 1045810 }, counts: { chunks: 8 } });
const DELTA = sse("delta", { text: "Nvidia reported " });
const DONE = sse("done", { answer: "Nvidia reported growth.", cached: true });
const SERVER_ERROR = sse("error", { detail: "The service is paused for today." });
const CUT = Symbol("cut");        // the next read() rejects, the way a reset or aborted connection does
const END = Symbol("end");        // the next read() reports a clean end of the stream

// A page whose /api/ask streams `steps` in order (bytes, CUT or END). Every other request (stats, examples, freshness)
// never settles, as the harness's own default fetch does.
function askPage(steps) {
  const remaining = [...steps];
  let reads = 0, askDisabledWhileStreaming = null;
  const page = loadPage({
    fetch: (url) => (url === "/api/ask"
      ? Promise.resolve({ ok: true, body: { getReader: () => ({ read: async () => {
          reads += 1;
          askDisabledWhileStreaming ??= page.document.getElementById("ask").disabled;
          const step = remaining.shift() ?? END;
          if (step === CUT) throw new TypeError("network error");
          return step === END ? { done: true, value: undefined } : { done: false, value: step };
        } }) } })
      : new Promise(() => {})),
  });
  const el = (id) => page.document.getElementById(id);
  el("q").value = "What changed in Nvidia's risk factors?"; el("strategy").value = "hybrid";
  return { ...page, el, reads: () => reads, askDisabledWhileStreaming: () => askDisabledWhileStreaming };
}

test("a read that rejects mid-answer says the connection was lost and enables Ask again", async () => {
  const p = askPage([RETRIEVAL, DELTA, CUT]);
  await p.api.ask();
  assert.equal(p.askDisabledWhileStreaming(), true, "Ask is disabled while the answer streams (else this test proves nothing)");
  assert.equal(p.el("ask").disabled, false);
  assert.match(p.el("status").innerHTML, /<span class="err">connection lost/);
  assert.doesNotMatch(p.el("status").innerHTML, /generating/);
  assert.match(p.el("answer").innerHTML, /Nvidia reported/, "the text that did arrive stays on the page");
  assert.equal(p.el("meta").hidden, true, "an unfinished answer carries no checks footer");
});

test("the status never claims the cut answer is complete or checked", async () => {
  const p = askPage([DELTA, CUT]);
  await p.api.ask();
  assert.match(p.el("status").innerHTML, /incomplete/);
  assert.match(p.el("status").innerHTML, /not checked/);
});

test("a stream that ends cleanly without a done or error event is reported the same way", async () => {
  const p = askPage([RETRIEVAL, DELTA, END]);
  await p.api.ask();
  assert.equal(p.el("ask").disabled, false);
  assert.match(p.el("status").innerHTML, /<span class="err">connection lost/);
  assert.doesNotMatch(p.el("status").innerHTML, /generating/);
});

test("a cut before the first byte is reported too", async () => {
  const p = askPage([CUT]);
  await p.api.ask();
  assert.equal(p.el("ask").disabled, false);
  assert.match(p.el("status").innerHTML, /<span class="err">connection lost/);
});

for (const ending of [CUT, END]) {
  test(`a finished answer keeps its own status when the stream then ends by ${ending === CUT ? "a reset" : "closing"}`, async () => {
    const p = askPage([DELTA, DONE, ending]);
    await p.api.ask();
    assert.equal(p.el("ask").disabled, false);
    assert.equal(p.el("status").innerHTML, "served from cache — no model call");
    assert.equal(p.el("meta").hidden, false);
    assert.match(p.el("answer").innerHTML, /Nvidia reported growth\./);
  });

  test(`the server's own error detail is kept when the stream then ends by ${ending === CUT ? "a reset" : "closing"}`, async () => {
    const p = askPage([RETRIEVAL, SERVER_ERROR, ending]);
    await p.api.ask();
    assert.equal(p.el("ask").disabled, false);
    assert.match(p.el("status").innerHTML, /<span class="err">The service is paused for today\.<\/span>/);
    assert.doesNotMatch(p.el("status").innerHTML, /connection lost/);
  });
}

test("an ordinary answer is unchanged: done status, checks footer, Ask enabled", async () => {
  const p = askPage([RETRIEVAL, DELTA, sse("done", { answer: "Nvidia reported growth.", cached: false }), END]);
  await p.api.ask();
  assert.equal(p.el("status").innerHTML, "done");
  assert.equal(p.el("meta").hidden, false);
  assert.equal(p.el("ask").disabled, false);
});

test("a failure while handling an event still enables Ask again", async () => {
  const p = askPage([DELTA, DONE, END]);
  p.context.renderMarkdown = () => { throw new Error("boom"); };   // a page-level function the event handler calls
  await p.api.ask().catch(() => {});
  assert.equal(p.el("ask").disabled, false);
});

// The page itself can throw while it takes an answer in (a rendering bug, a malformed event). Ask used to come back, but
// the status stayed on "retrieving…" / "… generating…" for good, which says the answer is still on its way. Every way out
// of ask() must leave a status that is true: here, that the page could not show the answer completely.
const PAGE_FAILED = /<span class="err">the page could not show this answer completely/;

function assertHonestAfterAPageFailure(p, why) {
  assert.equal(p.el("ask").disabled, false, `${why}: Ask is enabled again`);
  assert.match(p.el("status").innerHTML, PAGE_FAILED, `${why}: the status says the page failed`);
  assert.doesNotMatch(p.el("status").innerHTML, /generating|retrieving|^done$/, `${why}: never a status that is not true`);
  assert.match(p.el("status").innerHTML, /not checked/, `${why}: it does not claim the checks ran`);
}

test("a failure while rendering a delta leaves an honest status, not generating…", async () => {
  const p = askPage([RETRIEVAL, DELTA, DONE, END]);
  p.context.renderMarkdown = () => { throw new Error("boom"); };
  await p.api.ask().catch(() => {});
  assertHonestAfterAPageFailure(p, "delta");
});

test("a failure while rendering the finished answer does not leave the status on generating…", async () => {
  const p = askPage([RETRIEVAL, DELTA, DONE, END]);
  const render = p.context.renderMarkdown;
  p.context.renderMarkdown = (t) => { if (t === "Nvidia reported growth.") throw new Error("boom"); return render(t); };
  await p.api.ask().catch(() => {});
  assertHonestAfterAPageFailure(p, "done");
  assert.match(p.el("answer").innerHTML, /Nvidia reported/, "the text that did arrive stays on the page");
});

test("a failure after the done status was set does not leave 'done' over an answer whose checks were never shown", async () => {
  const p = askPage([RETRIEVAL, DELTA, DONE, END]);
  p.context.metaHtml = () => { throw new Error("boom"); };        // finish() sets "done", then builds the checks footer
  await p.api.ask().catch(() => {});
  assertHonestAfterAPageFailure(p, "finish");
  assert.equal(p.el("meta").hidden, true, "no footer was built");
});

test("a response without a body is reported and enables Ask again", async () => {
  const page = loadPage({ fetch: (url) => (url === "/api/ask" ? Promise.resolve({ ok: true, body: null }) : new Promise(() => {})) });
  page.document.getElementById("q").value = "What changed in Nvidia's risk factors?";
  page.document.getElementById("strategy").value = "hybrid";
  await page.api.ask().catch(() => {});
  assert.equal(page.document.getElementById("ask").disabled, false);
  assert.match(page.document.getElementById("status").innerHTML, PAGE_FAILED);
  assert.doesNotMatch(page.document.getElementById("status").innerHTML, /retrieving/);
});

test("the page failure is not hidden: ask() still rejects with the original error", async () => {
  const p = askPage([DELTA, END]);
  p.context.renderMarkdown = () => { throw new Error("boom"); };
  await assert.rejects(p.api.ask(), /boom/);
});

test("a server error event that was shown is not replaced by the page-failure text", async () => {
  const p = askPage([RETRIEVAL, SERVER_ERROR, END]);
  await p.api.ask();
  assert.match(p.el("status").innerHTML, /<span class="err">The service is paused for today\.<\/span>/);
  assert.doesNotMatch(p.el("status").innerHTML, /could not show/);
});
