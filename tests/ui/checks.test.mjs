// The review's checks on the page: numbers_checked, echoed figures, uncited answers and unsupported removal claims.
// A badge may only say what the checks prove, and never shows green for a check that examined nothing.
import assert from "node:assert/strict";
import test from "node:test";
import { loadPage, plain } from "./harness.mjs";

const { api } = loadPage();
const SONNET = "anthropic/claude-sonnet-5";
const CLEAN = {
  citations_retrieved: true, numbers_grounded: true, numbers_checked: 2, unmatched_numbers: [], echoed_numbers: [],
  pseudo_citations: [], has_citation: true, is_refusal: false, unsupported_removal_claim: false, unsupported_removal_sentences: [],
};
const summary = (checks, extra = {}) => plain(api.checksSummary({ citations: ["a"], hallucinated: [], checks, ...extra }));
const texts = (s) => s.badges.map((b) => b.text);
const badge = (s, re) => s.badges.find((b) => re.test(b.text));

// ---- numbers_checked

test("with figures examined and all grounded the numbers badge is green", () => {
  const b = badge(summary(CLEAN), /numbers matched the retrieved context/);
  assert.ok(b && b.cls === "ok");
});

test("with no figure to check the badge is neutral and never green", () => {
  const s = summary({ ...CLEAN, numbers_checked: 0 });
  const b = badge(s, /no figures to check/);
  assert.ok(b, texts(s).join(" | "));
  assert.equal(b.cls, "");
  assert.ok(!texts(s).some((t) => /matched the retrieved context/.test(t)));
});

test("an old payload without numbers_checked still reads 'numbers not checked', not 'no figures to check'", () => {
  const { numbers_checked, ...old } = CLEAN;
  const s = summary(old);
  assert.ok(texts(s).includes("numbers not checked"));
  assert.ok(!texts(s).some((t) => /matched the retrieved context|no figures to check/.test(t)));
  assert.ok(texts(summary(undefined)).includes("numbers not checked"));
});

// ---- echoed figures

test("a figure taken from the question is a warning, not a match", () => {
  const s = summary({ ...CLEAN, numbers_grounded: false, echoed_numbers: ["$500 billion"] });
  assert.ok(!texts(s).some((t) => /matched the retrieved context/.test(t)));
  const b = badge(s, /taken from your question/);
  assert.ok(b && b.cls === "warn");
  assert.match(s.warnings.join("\n"), /A figure taken from your question, not verified: \$500 billion/);
});

test("echoed and unmatched figures are reported separately", () => {
  const s = summary({ ...CLEAN, numbers_grounded: false, echoed_numbers: ["$500 billion"], unmatched_numbers: ["$190 billion"] });
  const all = s.warnings.join("\n");
  assert.match(all, /Numbers not found in the retrieved context: \$190 billion/);
  assert.match(all, /taken from your question, not verified: \$500 billion/);
  assert.ok(s.badges.some((b) => b.cls === "bad" && /numbers/.test(b.text)));
});

// ---- uncited answers

test("an answer with no citation that is not a refusal never shows the green cited-ids badge", () => {
  const s = summary({ ...CLEAN, has_citation: false, numbers_checked: 0 }, { citations: [] });
  assert.ok(!texts(s).includes("cited ids were retrieved"));
  const b = badge(s, /no citation/i);
  assert.ok(b && b.cls === "warn", texts(s).join(" | "));
  assert.match(s.warnings.join("\n"), /cites no source/i);
});

test("a zero-citation refusal is fine: neutral, no warning, and not green either", () => {
  const s = summary({ ...CLEAN, has_citation: false, is_refusal: true, numbers_checked: 0 }, { citations: [] });
  assert.ok(!texts(s).includes("cited ids were retrieved"));
  assert.ok(s.badges.every((b) => b.cls !== "warn" && b.cls !== "bad"), texts(s).join(" | "));
  assert.deepEqual(s.warnings, []);
});

test("a payload with zero citations and no checks at all is never green about citations", () => {
  const s = plain(api.checksSummary({ citations: [], hallucinated: [] }));
  assert.ok(!texts(s).includes("cited ids were retrieved"));
});

test("cited answers keep the green cited-ids badge", () => {
  assert.ok(texts(summary(CLEAN)).includes("cited ids were retrieved"));
});

// ---- removal claims

test("an unsupported removal claim is a warning badge and names the sentence", () => {
  const s = summary({ ...CLEAN, unsupported_removal_claim: true, unsupported_removal_sentences: ["Nvidia dropped its export-control risk."] });
  const b = badge(s, /removal/i);
  assert.ok(b && b.cls === "warn");
  assert.match(s.warnings.join("\n"), /"no longer appears" lists/);
  assert.match(s.warnings.join("\n"), /Nvidia dropped its export-control risk\./);
  assert.doesNotMatch(b.text, /removed lists/);
});

test("the checks hint points at the change-claim reliability note and never calls the comparison verified", () => {
  const s = summary(CLEAN);
  assert.match(s.hint, /automatic text comparison/);
  assert.match(s.hint, /How reliable are change claims\?/);
  assert.doesNotMatch(s.hint, /text-verified|verified absent/);
});

test("a removal claim flag without sentences still warns", () => {
  const s = summary({ ...CLEAN, unsupported_removal_claim: true });
  assert.ok(badge(s, /removal/i));
});

// ---- the badge on the answer, and the escalation reasons

test("a cheap draft released with any of the new failures is not called a pass", () => {
  for (const failing of [{ has_citation: false }, { echoed_numbers: ["$5"], numbers_grounded: false },
                         { unsupported_removal_claim: true }]) {
    const b = plain(api.answerBadge({ routed: "cheap", escalated: false, answered_by: SONNET, checks: { ...CLEAN, ...failing } }));
    assert.doesNotMatch(b.text, /passed the checks/);
    assert.equal(b.cls, "warn");
  }
  assert.equal(plain(api.answerBadge({ routed: "cheap", escalated: false, answered_by: SONNET, checks: CLEAN })).text,
    "claude-sonnet-5 draft passed the checks");
});

test("checksPassed is one predicate: a refusal with no citation passes, an uncited claim does not", () => {
  assert.equal(api.checksPassed(CLEAN), true);
  assert.equal(api.checksPassed({ ...CLEAN, has_citation: false, is_refusal: true }), true);
  assert.equal(api.checksPassed({ ...CLEAN, has_citation: false, is_refusal: false }), false);
  assert.equal(api.checksPassed({ ...CLEAN, echoed_numbers: ["$1"] }), false);
  assert.equal(api.checksPassed({ ...CLEAN, unsupported_removal_claim: true }), false);
  assert.equal(api.checksPassed({ citations_retrieved: true }), true);       // an older payload: nothing it predates is failed
  assert.equal(api.checksPassed(undefined), true);
});

test("the new escalation reasons read in plain words", () => {
  assert.match(api.reasonText("unsupported_removal_claim"), /removal claim/);
  assert.match(api.reasonText("unsupported_removal_claim"), /"no longer appears" lists/);
  assert.match(api.reasonText("ungrounded_number"), /number not found in the retrieved context/);
  assert.match(api.escalationStatus({ reasons: ["unsupported_removal_claim"] }), /removal claim/);
});

// ---- escaping

test("sentences and figures from the server are escaped in the new warnings", () => {
  const html = api.metaHtml({
    citations: [], hallucinated: [],
    checks: { ...CLEAN, has_citation: false, numbers_grounded: false, echoed_numbers: ["<img src=x onerror=alert(1)>"],
              unsupported_removal_claim: true, unsupported_removal_sentences: ["<script>alert(2)</script> was removed"] },
  });
  assert.doesNotMatch(html, /<script/i);
  assert.doesNotMatch(html, /<img/i);
  assert.match(html, /&lt;script&gt;/);
});

// ---- the example panel

test("with no saved example the panel says so instead of listing questions that would cost money", () => {
  const html = api.examplesHtml({ source: "s", examples: [] });
  assert.match(html, /No saved example answers/);
  assert.doesNotMatch(html, /<button/);
  assert.doesNotThrow(() => api.examplesHtml(undefined));
  assert.doesNotThrow(() => api.examplesHtml({}));
});

test("examples are grouped by type and escaped", () => {
  const html = api.examplesHtml({ examples: [
    { id: "N1", type: "numeric", question: "Revenue?" }, { id: "N2", type: "numeric", question: '"><b>x</b>' },
    { id: "U1", type: "refusal", question: "Samsung?" }] });
  assert.equal((html.match(/<button/g) || []).length, 3);
  assert.equal((html.match(/class="t"/g) || []).length, 2);
  assert.doesNotMatch(html, /<b>x/);
});

// ---- what "checked" covers is stated, so "no figures to check" is never read as "the answer has no numbers"

test("the hint says which figures are checked and that a bare number is not", () => {
  const s = summary(CLEAN);
  assert.match(s.hint, /do not prove/);
  assert.match(s.hint, /currency/);
  assert.match(s.hint, /bare number/);
  assert.match(s.hint, /only your question states/);
});
