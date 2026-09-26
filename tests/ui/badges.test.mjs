// The answer badge and the checks summary must say what actually happened, and never more than the checks prove.
import assert from "node:assert/strict";
import test from "node:test";
import { loadPage, plain } from "./harness.mjs";

const { api } = loadPage();
const LUNA = "openai/gpt-6-luna";
const SONNET = "anthropic/claude-sonnet-5";

test("a routed answer is never described as a verified draft", () => {
  const b = plain(api.answerBadge({ routed: "strong", escalated: false, answered_by: SONNET }));
  assert.match(b.text, /answered by claude-sonnet-5/);
  assert.match(b.text, /routed: change-over-time question; no cheap draft/);
  assert.doesNotMatch(b.text, /draft (was )?verified|draft passed/i);
});

test("an escalated answer names the failed checks in plain words", () => {
  const b = plain(api.answerBadge({
    routed: "cheap", escalated: true, answered_by: SONNET,
    escalation_reasons: ["ungrounded_number", "invalid_citation"],
  }));
  assert.match(b.text, /answered by claude-sonnet-5/);
  assert.match(b.text, /escalated after a failed check/);
  assert.match(b.text, /number not found in the retrieved context/);
  assert.match(b.text, /cited id was not in the retrieved context/);
});

test("an unknown escalation reason falls back to a readable form", () => {
  assert.equal(api.reasonText("some_new_reason"), "some new reason");
});

test("a cheap draft that passed says so, and only when no check flagged anything", () => {
  const ok = plain(api.answerBadge({
    routed: "cheap", escalated: false, answered_by: LUNA,
    checks: { citations_retrieved: true, numbers_grounded: true, unmatched_numbers: [], pseudo_citations: [] },
  }));
  assert.equal(ok.text, "gpt-6-luna draft passed the checks");
  const flagged = plain(api.answerBadge({
    routed: "cheap", escalated: false, answered_by: LUNA,
    checks: { citations_retrieved: true, numbers_grounded: false, unmatched_numbers: ["$9.9B"], pseudo_citations: [] },
  }));
  assert.doesNotMatch(flagged.text, /passed the checks/);
  assert.equal(flagged.cls, "warn");
});

test("with the routing fields missing the badge claims nothing about verification", () => {
  const b = plain(api.answerBadge({ answered_by: LUNA }));
  assert.equal(b.text, "answered by gpt-6-luna");
});

test("a live stream without escalation falls back to the configured answer model", () => {
  const b = plain(api.answerBadge({ cached: false }, SONNET));
  assert.equal(b.text, "answered by claude-sonnet-5 (streamed live)");
  assert.equal(api.answerBadge({ cached: false }), null);
  assert.equal(api.answerBadge(null), null);
});

test("no badge text anywhere says 'draft verified' or 'all citations verified'", () => {
  const events = [
    { routed: "strong", answered_by: SONNET },
    { routed: "cheap", escalated: true, answered_by: SONNET, escalation_reasons: ["no_citation"] },
    { routed: "cheap", escalated: false, answered_by: LUNA, checks: { citations_retrieved: true, numbers_grounded: true } },
    { answered_by: LUNA },
  ];
  for (const ev of events) {
    const html = api.metaHtml({ ...ev, citations: ["a"], hallucinated: [], usage: {}, cost_usd: 0.001 });
    assert.doesNotMatch(html, /draft verified/i);
    assert.doesNotMatch(html, /all citations verified/i);
  }
});

test("checks that all passed produce honest wording and no warnings", () => {
  const s = plain(api.checksSummary({
    citations: ["x", "y"], hallucinated: [],
    checks: { citations_retrieved: true, numbers_grounded: true, unmatched_numbers: [], pseudo_citations: [] },
  }));
  const texts = s.badges.map((b) => b.text);
  assert.ok(texts.includes("2 citations"));
  assert.ok(texts.includes("cited ids were retrieved"));
  assert.ok(texts.includes("numbers matched the retrieved context"));
  assert.deepEqual(s.warnings, []);
  assert.ok(texts.every((t) => !/verified/i.test(t)));
  assert.match(s.hint, /do not prove/);
});

test("unmatched numbers and pseudo-citations become visible warnings", () => {
  const s = plain(api.checksSummary({
    citations: ["x"], hallucinated: [],
    checks: {
      citations_retrieved: true, numbers_grounded: false,
      unmatched_numbers: ["$12.9B", "43%"], pseudo_citations: ["[Reported Metrics]"],
    },
  }));
  assert.ok(s.badges.some((b) => b.cls === "bad" && /numbers/.test(b.text)));
  const all = s.warnings.join("\n");
  assert.match(all, /\$12\.9B/);
  assert.match(all, /43%/);
  assert.match(all, /\[Reported Metrics\]/);
});

test("a cited id outside the retrieved context is flagged as bad", () => {
  const s = plain(api.checksSummary({
    citations: ["a", "b"], hallucinated: ["b"], checks: { citations_retrieved: false },
  }));
  assert.ok(s.badges.some((b) => b.cls === "bad" && /not in the retrieved context/.test(b.text)));
  assert.match(s.warnings.join("\n"), /b/);
});

test("answers without a checks object (cached, older) do not claim their numbers were checked", () => {
  const s = plain(api.checksSummary({ citations: ["a"], hallucinated: [] }));
  const texts = s.badges.map((b) => b.text);
  assert.ok(texts.includes("cited ids were retrieved"));
  assert.ok(texts.includes("numbers not checked"));
  assert.ok(!texts.some((t) => /matched the retrieved context/.test(t)));
});

test("a missing or malformed event never throws", () => {
  assert.doesNotThrow(() => api.checksSummary({}));
  assert.doesNotThrow(() => api.checksSummary({ citations: "nope", hallucinated: null, checks: "x" }));
  assert.doesNotThrow(() => api.metaHtml({}));
  assert.doesNotThrow(() => api.metaHtml({ usage: null, cost_usd: "free", citations: null }));
});

test("server-supplied strings are escaped inside the badges and warnings", () => {
  const html = api.metaHtml({
    citations: [], hallucinated: [], routed: "cheap", answered_by: "x/<img src=x onerror=alert(1)>",
    checks: { unmatched_numbers: ["<script>alert(1)</script>"], pseudo_citations: ['"><b>x</b>'] },
  });
  assert.doesNotMatch(html, /<script/i);
  assert.doesNotMatch(html, /<img/i);
  assert.doesNotMatch(html, /<b>x/);
});

test("a truncated live answer is flagged, a cached one says it was saved", () => {
  assert.match(api.metaHtml({ finish_reason: "length", citations: [] }), /truncated by the token budget/);
  const cached = api.metaHtml({ cached: true, source: "benchmark", citations: [] });
  assert.match(cached, /saved example answer/);
  assert.doesNotMatch(cached, /benchmarked answer/);
});

test("the escalation status names the reasons the draft was rejected", () => {
  const s = api.escalationStatus({ reasons: ["ungrounded_number", "draft_error"], from: "openai/gpt-6-luna", to: SONNET });
  assert.match(s, /did not pass the automatic checks/);
  assert.match(s, /number not found in the retrieved context/);
  assert.match(s, /draft model failed/);
  assert.doesNotMatch(api.escalationStatus({}), /undefined|\(\)/);
});
