// The opt-in "Deep research (agent)" UI: the option, the plain-language step timeline and the privacy line.
// Nothing agent-related may show while the service reports `agent_enabled: false`, and server text is only ever shown through
// textContent (a step's summary is server text: counts, ids, fiscal years; a hostile one must render as inert text).
import assert from "node:assert/strict";
import test from "node:test";
import { loadPage } from "./harness.mjs";

// A minimal element: enough for the page's agent code, and a trap on innerHTML (server text must never reach it).
class Fake {
  constructor(tag = "div") { this.tag = tag; this.children = []; this.parent = null; this.hidden = false; this.value = ""; this._text = ""; }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); this.children = []; }
  set innerHTML(_) { throw new Error("innerHTML must not be used for agent output"); }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); }
  querySelector(selector) {
    const m = selector.match(/^option\[value="([a-z]+)"\]$/);
    return m ? this.children.find((c) => c.tag === "option" && c.value === m[1]) || null : null;
  }
  get options() { return this.children.filter((c) => c.tag === "option"); }
  // FIX 3 (owner's live G10 test, 2026-09-30): the page listens for a "change" on #strategy; `fireChange` simulates
  // the user picking an option, the same way tests/ui/upload_flow.test.mjs's selectStub does for askAsOf selects.
  addEventListener(evt, fn) { if (evt === "change") this._onChange = fn; }
  fireChange(value) { this.value = value; if (this._onChange) this._onChange(); }
}

function pageWithAgentElements() {
  const strategy = new Fake("select");
  for (const value of ["hybrid", "vector"]) { const o = new Fake("option"); o.value = value; strategy.appendChild(o); }
  strategy.value = "hybrid";
  const elements = { strategy, agentSteps: new Fake("ol"), agentHint: new Fake(), tracingNote: new Fake() };
  elements.agentSteps.hidden = elements.agentHint.hidden = elements.tracingNote.hidden = true;
  return { ...loadPage({ elements, createElement: (tag) => new Fake(tag) }), elements };
}

const STEP = (over) => ({ event: "step", n: 1, tool: "lookup_company", args: {}, summary: "Nvidia", ok: true, ...over });

// ---- the plain-language line of one step

test("a step reads as a short plain sentence built from the tool and the summary", () => {
  const { api } = loadPage();
  assert.equal(api.stepText(STEP()), "Looked up Nvidia");
  assert.equal(api.stepText(STEP({ tool: "financial_metrics", summary: "revenue for FY2024-2026" })), "Fetched revenue for FY2024-2026");
  assert.equal(api.stepText(STEP({ tool: "search_filings", summary: "6 excerpts" })), "Searched the filings: 6 excerpts");
  assert.equal(api.stepText(STEP({ tool: "risk_changes", summary: "Nvidia FY2025 to FY2026" })), "Compared risk disclosures: Nvidia FY2025 to FY2026");
  assert.equal(api.stepText(STEP({ tool: "relationships", summary: "12 links" })), "Checked relationships: 12 links");
  assert.equal(api.stepText(STEP({ tool: "active_risks", summary: "9 items" })), "Listed active risks: 9 items");
  assert.equal(api.stepText(STEP({ tool: "compute_change", summary: "revenue change FY2026 vs FY2025" })), "Computed revenue change FY2026 vs FY2025");
});

test("a failed step says so, a step without a summary is just the verb, an unknown tool gets a generic verb", () => {
  const { api } = loadPage();
  assert.equal(api.stepText(STEP({ tool: "search_filings", summary: "no excerpts", ok: false })), "Searched the filings: no excerpts (failed)");
  assert.equal(api.stepText(STEP({ summary: "" })), "Looked up");
  assert.equal(api.stepText(STEP({ tool: "brand_new_tool", summary: "3 items" })), "Ran brand new tool: 3 items");
  assert.equal(api.stepText(STEP({ tool: "<img src=x onerror=alert(1)>" })), "Ran a tool: Nvidia");
  assert.equal(api.stepText(STEP({ tool: "constructor" })), "Ran constructor: Nvidia");   // an Object.prototype name is not a known tool
  assert.equal(api.stepText(STEP({ tool: "__proto__" })), "Ran a tool: Nvidia");
});

test("a step is defensive about the shape of what the server sent", () => {
  const { api } = loadPage();
  assert.equal(api.stepText(null), "Ran a tool");
  assert.equal(api.stepText({}), "Ran a tool");
  assert.equal(api.stepText(STEP({ summary: { evil: 1 } })), "Looked up");
  assert.equal(api.stepText(STEP({ summary: "line one\n\n  line two\t!" })), "Looked up line one line two !");
  const long = api.stepText(STEP({ summary: "x".repeat(500) }));
  assert.ok(long.length < 200 && long.endsWith("…"));
});

test("server text is returned as plain text for textContent, never escaped into markup and never interpreted", () => {
  const { api } = loadPage();
  const hostile = "<b>bold</b> & <script>alert(1)</script>";
  assert.equal(api.stepText(STEP({ summary: hostile })), `Looked up ${hostile}`);
});

// ---- the option, the hint and the privacy line

test("agentEnabled is true only for an explicit true", () => {
  const { api } = loadPage();
  assert.equal(api.agentEnabled({ agent_enabled: true }), true);
  for (const s of [{}, { agent_enabled: false }, { agent_enabled: "true" }, { agent_enabled: 1 }, null, undefined]) assert.equal(api.agentEnabled(s), false);
});

test("with the agent off nothing agent-related is shown", () => {
  const { api, elements } = pageWithAgentElements();
  api.syncAgentUi({ agent_enabled: false, tracing: true });
  assert.deepEqual(elements.strategy.options.map((o) => o.value), ["hybrid", "vector"]);
  assert.equal(elements.agentHint.hidden, true); assert.equal(elements.agentHint.textContent, "");
  assert.equal(elements.tracingNote.hidden, true); assert.equal(elements.tracingNote.textContent, "");
  assert.equal(elements.agentSteps.hidden, true);
});

test("with the agent on the option appears once, however often the stats are reloaded, without a privacy line", () => {
  const { api, elements } = pageWithAgentElements();
  for (let i = 0; i < 3; i++) api.syncAgentUi({ agent_enabled: true, tracing: false });   // loadStats() runs after every answer
  const agent = elements.strategy.options.filter((o) => o.value === "agent");
  assert.equal(agent.length, 1);
  assert.match(agent[0].textContent, /^Deep research \(agent\)/);
  assert.equal(elements.tracingNote.hidden, true);
});

// ---- the hint tracks the SELECTED strategy, not just whether the agent is enabled (owner's live G10 test, 2026-09-30):
// it must not describe the agent while the user is looking at a hybrid answer.

test("the hint stays hidden for the default hybrid strategy even while the agent is enabled", () => {
  const { api, elements } = pageWithAgentElements();
  api.syncAgentUi({ agent_enabled: true });
  assert.equal(elements.strategy.value, "hybrid");
  assert.equal(elements.agentHint.hidden, true); assert.equal(elements.agentHint.textContent, "");
});

test("picking the agent strategy shows the hint; switching back to hybrid hides it again", () => {
  const { api, elements } = pageWithAgentElements();
  api.syncAgentUi({ agent_enabled: true });
  elements.strategy.fireChange("agent");
  assert.equal(elements.agentHint.hidden, false);
  assert.match(elements.agentHint.textContent, /planning model|planner/i);
  elements.strategy.fireChange("hybrid");
  assert.equal(elements.agentHint.hidden, true); assert.equal(elements.agentHint.textContent, "");
});

test("clicking a benchmark example (which forces hybrid) hides the hint at once, not only after the answer", () => {
  const { api, elements } = pageWithAgentElements();
  api.syncAgentUi({ agent_enabled: true });
  elements.strategy.fireChange("agent");
  assert.equal(elements.agentHint.hidden, false);
  api.askExample("What was Nvidia's total revenue for the fiscal year ended January 25, 2026?");
  assert.equal(elements.strategy.value, "hybrid");
  assert.equal(elements.agentHint.hidden, true); assert.equal(elements.agentHint.textContent, "");
});

test("syncAgentUi stays idempotent when re-run (as loadStats does after every answer) with the agent strategy selected", () => {
  const { api, elements } = pageWithAgentElements();
  api.syncAgentUi({ agent_enabled: true });
  elements.strategy.fireChange("agent");
  for (let i = 0; i < 3; i++) api.syncAgentUi({ agent_enabled: true });
  assert.equal(elements.agentHint.hidden, false);
  assert.equal(elements.strategy.options.filter((o) => o.value === "agent").length, 1);
});

test("the hint is hidden the moment the service reports the agent off, even with agent selected", () => {
  const { api, elements } = pageWithAgentElements();
  api.syncAgentUi({ agent_enabled: true });
  elements.strategy.fireChange("agent");
  assert.equal(elements.agentHint.hidden, false);
  api.syncAgentUi({ agent_enabled: false });
  assert.equal(elements.agentHint.hidden, true); assert.equal(elements.agentHint.textContent, "");
});

test("the privacy line appears only when a sample of agent questions is really traced, and never claims the text is sent", () => {
  const { api, elements } = pageWithAgentElements();
  api.syncAgentUi({ agent_enabled: true, tracing: true });
  assert.equal(elements.tracingNote.hidden, false);
  const note = elements.tracingNote.textContent;
  // Exactly what the tracer sends (tests/test_tracing.py): lengths and a one-way hash, tool and model names, tokens, cost, timings,
  // the fallback reason; never the text, never an address. "Sampling rate", not "a small sample": the rate is the operator's.
  assert.match(note, /sampling rate/); assert.doesNotMatch(note, /small sample/);
  for (const part of [/length of the question/, /one-way hash/, /tools and models/, /token counts/, /cost/, /timings/, /fallback reason/]) assert.match(note, part);
  assert.match(note, /never the text of the question or the answer, and never your address/);
  assert.equal(api.tracingNote({ agent_enabled: true, tracing: false }), "");
  assert.equal(api.tracingNote({ agent_enabled: false, tracing: true }), "");
});

test("turning the agent off again removes the option, resets a selected agent to hybrid and clears the timeline", () => {
  const { api, elements } = pageWithAgentElements();
  api.syncAgentUi({ agent_enabled: true });
  elements.strategy.value = "agent";
  api.addStep(STEP());
  api.syncAgentUi({ agent_enabled: false });
  assert.deepEqual(elements.strategy.options.map((o) => o.value), ["hybrid", "vector"]);
  assert.equal(elements.strategy.value, "hybrid");
  assert.equal(elements.agentSteps.hidden, true); assert.equal(elements.agentSteps.children.length, 0);
});

test("the option keeps a chosen strategy that is not the agent", () => {
  const { api, elements } = pageWithAgentElements();
  elements.strategy.value = "vector";
  api.syncAgentUi({ agent_enabled: true }); api.syncAgentUi({ agent_enabled: true });
  assert.equal(elements.strategy.value, "vector");
});

// ---- the timeline

test("each step is one list item set through textContent, and the list becomes visible with the first step", () => {
  const { api, elements } = pageWithAgentElements();
  api.addStep(STEP());
  api.addStep(STEP({ n: 2, tool: "financial_metrics", summary: "revenue for FY2024-2026" }));
  assert.equal(elements.agentSteps.hidden, false);
  assert.deepEqual(elements.agentSteps.children.map((li) => [li.tag, li.textContent]),
    [["li", "Looked up Nvidia"], ["li", "Fetched revenue for FY2024-2026"]]);
});

test("a hostile summary lands in textContent, so it is displayed as text and cannot run", () => {
  const { api, elements } = pageWithAgentElements();
  api.addStep(STEP({ summary: "<img src=x onerror=alert(1)>" }));          // the Fake throws if innerHTML is ever touched
  assert.equal(elements.agentSteps.children[0].textContent, "Looked up <img src=x onerror=alert(1)>");
});

test("clearing the timeline empties and hides it, and a new answer starts from step one", () => {
  const { api, elements } = pageWithAgentElements();
  api.addStep(STEP()); api.clearSteps();
  assert.equal(elements.agentSteps.hidden, true); assert.equal(elements.agentSteps.children.length, 0);
  api.addStep(STEP({ summary: "AMD" }));
  assert.deepEqual(elements.agentSteps.children.map((li) => li.textContent), ["Looked up AMD"]);
});

test("the timeline is bounded whatever the server streams", () => {
  const { api, elements } = pageWithAgentElements();
  for (let i = 0; i < 50; i++) api.addStep(STEP({ n: i + 1 }));
  assert.ok(elements.agentSteps.children.length <= 12);
});
