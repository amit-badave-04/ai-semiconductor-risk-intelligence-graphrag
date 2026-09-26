// Page copy that depends on live data: the stats line, the model/limits line and the cost note.
import assert from "node:assert/strict";
import test from "node:test";
import { loadPage } from "./harness.mjs";

const { api } = loadPage();

const STATS = {
  graph: { nodes: { Company: 26, Filing: 74, EvidenceSpan: 3152, RiskFactor: 9775, ExportControl: 166 }, removed_risk_items: 41, risk_items: 900 },
  ledger: { today: { paid: 3 } }, limits: { max_queries_per_day: 150, per_ip: "5 per 10 min" }, paused: false,
  models: { llm: "openai/gpt-6-luna", escalation: "anthropic/claude-sonnet-5" },
};

test("the stats line counts text-verified removed risk items", () => {
  const html = api.statsHtml(STATS);
  assert.match(html, /<b>41<\/b> text-verified removed risk items/);
  assert.doesNotMatch(html, /dropped risk lineages/);
  assert.match(html, /<b>74<\/b> filings/);
  assert.match(html, /<b>166<\/b> BIS rules/);
});

test("the removed-item count is omitted when the API does not report it", () => {
  const legacy = { ...STATS, graph: { ...STATS.graph, removed_risk_items: undefined, deleted_risk_lineages: 495 } };
  const html = api.statsHtml(legacy);
  assert.doesNotMatch(html, /removed risk items/);
  assert.doesNotMatch(html, /495/, "the old edge count must never be shown as lineages");
  assert.doesNotMatch(html, /undefined|NaN/);
});

test("a zero count is shown, not treated as missing", () => {
  const html = api.statsHtml({ ...STATS, graph: { ...STATS.graph, removed_risk_items: 0 } });
  assert.match(html, /<b>0<\/b> text-verified removed risk items/);
});

test("a paused service is flagged and a sparse stats object does not throw", () => {
  assert.match(api.statsHtml({ ...STATS, paused: true }), /live questions paused/);
  assert.doesNotThrow(() => api.statsHtml({ graph: {}, ledger: {}, limits: {} }));
  assert.doesNotThrow(() => api.statsHtml({}));
});

test("the limits line names the answer and escalation models from /api/stats", () => {
  const t = api.limitsText(STATS);
  assert.match(t, /gpt-6-luna/);
  assert.match(t, /claude-sonnet-5/);
  assert.match(t, /5 per 10 min/);
  assert.doesNotMatch(t, /undefined/);
  assert.match(api.limitsText({ ...STATS, models: { llm: "anthropic/claude-sonnet-5", escalation: null } }), /claude-sonnet-5/);
});

test("the cost note is derived from the configured models, not hard-coded", () => {
  const two = api.costNote(STATS.models);
  assert.match(two, /gpt-6-luna/);
  assert.match(two, /claude-sonnet-5/);
  assert.doesNotMatch(two, /one Claude call/i);
  const one = api.costNote({ llm: "anthropic/claude-sonnet-5", escalation: null });
  assert.match(one, /one claude-sonnet-5 call/);
  assert.doesNotMatch(one, /second/);
  const unknown = api.costNote(undefined);
  assert.match(unknown, /one or two model calls/);
  assert.doesNotMatch(unknown, /undefined/);
});

test("the retrieval status describes risk-change items, not dropped lineages", () => {
  const s = api.retrievalStatus({
    anchors: { Nvidia: 1045810 }, counts: { edges: 79, metrics: 12, risks: 6, temporal: 17, chunks: 8 }, anchor_defaulted: false,
  });
  assert.match(s, /anchors: Nvidia/);
  assert.match(s, /79 relationships/);
  assert.match(s, /17 risk-change items/);
  assert.doesNotMatch(s, /dropped lineages/);
  assert.match(api.retrievalStatus({ anchors: {}, counts: {}, anchor_defaulted: true }), /no company detected; defaulting to Nvidia/);
  assert.doesNotThrow(() => api.retrievalStatus({}));
});

test("shortModel drops the provider prefix", () => {
  assert.equal(api.shortModel("openai/gpt-6-luna"), "gpt-6-luna");
  assert.equal(api.shortModel(undefined), "");
});
