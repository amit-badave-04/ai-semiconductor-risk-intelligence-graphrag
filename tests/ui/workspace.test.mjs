// The upload workspace panel's pure functions (M4 Worker C, docs/v2/M4_PLAN.md 4.2): doc chip labels, the stale
// class, job progress text, a document's version timeline, and the freshness header line. No DOM, no npm packages.
import assert from "node:assert/strict";
import test from "node:test";
import { loadPage } from "./harness.mjs";

const { api } = loadPage();
const DOC = "doc:0123456789ab:v2:0007";

// ---------------------------------------------------------------- docChipLabel

test("docChipLabel falls back to a generic label before evidence has loaded", () => {
  assert.equal(api.docChipLabel(DOC), "your document · v2 ¶0007");
});

test("docChipLabel uses the document's title once evidence is known", () => {
  assert.equal(api.docChipLabel(DOC, { title: "Q2 board memo.pdf" }), "Q2 board memo.pdf · v2 ¶0007");
});

test("docChipLabel returns plain text, like chipLabel — the caller escapes it for an HTML context", () => {
  // A document title is user-controlled (the upload form's `title` field); this label is safe in a
  // `.textContent` assignment either way, and a caller building HTML wraps it in esc() itself (evidenceView does).
  assert.equal(api.docChipLabel(DOC, { title: "<b>x</b>" }), "<b>x</b> · v2 ¶0007");
  assert.match(api.esc(api.docChipLabel(DOC, { title: "<b>x</b>" })), /&lt;b&gt;x&lt;\/b&gt;/);
});

// ---------------------------------------------------------------- evidenceView for a `doc:` id

test("evidenceView for a doc id uses the richer docChipLabel and escapes an untrusted title in metaHtml", () => {
  const view = api.evidenceView(DOC, { title: "<b>Q2</b> memo.pdf", text: "Our margin was 41.5%.", status: "current" });
  assert.equal(view.kind, "doc");
  assert.equal(view.label, "<b>Q2</b> memo.pdf · v2 ¶0007");        // plain text: safe for the .textContent title
  assert.match(view.metaHtml, /&lt;b&gt;Q2&lt;\/b&gt; memo\.pdf/);   // but escaped wherever it lands in innerHTML
  assert.doesNotMatch(view.metaHtml, /<b>Q2<\/b>/);
  assert.equal(view.text, "Our margin was 41.5%.");
});

test("evidenceView for a superseded doc chunk shows the same freshness badge a filing chunk does", () => {
  const view = api.evidenceView(DOC, { status: "superseded", superseded_by_version: 3, valid_to: "2026-01-01" });
  assert.match(view.freshHtml, /superseded by 3/);
  assert.match(view.freshHtml, /valid until 2026-01-01/);
});

// ---------------------------------------------------------------- staleClass

test("staleClass flags exactly the ids named in stale_citations", () => {
  assert.equal(api.staleClass(DOC, [DOC]), "stale");
  assert.equal(api.staleClass(DOC, ["doc:0123456789ab:v1:0001"]), "");
  assert.equal(api.staleClass(DOC, []), "");
  assert.equal(api.staleClass(DOC, undefined), "");
  assert.equal(api.staleClass(DOC, null), "");
});

// ---------------------------------------------------------------- jobStateText

test("jobStateText names each pipeline stage in plain words", () => {
  assert.equal(api.jobStateText({ state: "received" }), "queued");
  assert.equal(api.jobStateText({ state: "parsing" }), "reading the document");
  assert.equal(api.jobStateText({ state: "comparing" }), "comparing with the previous version");
  assert.equal(api.jobStateText({ state: "ready" }), "ready");
});

test("jobStateText shows embedding progress and an eta when given", () => {
  assert.equal(api.jobStateText({ state: "embedding", progress: { done: 3, total: 10, eta_s: 12.4 } }),
    "embedding 3/10 (~13s left)");
  assert.equal(api.jobStateText({ state: "embedding", progress: { done: 0, total: 5, eta_s: 0 } }), "embedding 0/5");
  assert.equal(api.jobStateText({ state: "embedding" }), "embedding");
});

test("jobStateText surfaces the fixed error message of a failed job, never a stack trace", () => {
  assert.equal(api.jobStateText({ state: "failed", error: { code: "too_many_pages", message: "too many pages" } }),
    "failed: too many pages");
  assert.equal(api.jobStateText({ state: "failed" }), "failed: unknown error");
});

test("jobStateText tolerates a missing or unknown job object", () => {
  assert.equal(api.jobStateText(null), "unknown");
  assert.equal(api.jobStateText({}), "unknown");
  assert.equal(api.jobStateText({ state: "some_future_state" }), "some_future_state");
});

// ---------------------------------------------------------------- versionTimeline

test("versionTimeline marks exactly the current version", () => {
  const versions = [{ version: 1, is_current: false, status: "superseded" },
                    { version: 2, is_current: true, status: "current" }];
  assert.equal(api.versionTimeline(versions), "v1 (superseded) → v2 (current)");
});

test("versionTimeline handles a single version and an empty list", () => {
  assert.equal(api.versionTimeline([{ version: 1, is_current: true, status: "current" }]), "v1 (current)");
  assert.equal(api.versionTimeline([]), "");
  assert.equal(api.versionTimeline(undefined), "");
});

// ---------------------------------------------------------------- relativeTime / freshnessLine

test("relativeTime reports minutes, hours and days ago", () => {
  const now = Date.parse("2026-09-29T12:00:00Z");
  assert.equal(api.relativeTime("2026-09-29T11:59:55Z", now), "just now");
  assert.equal(api.relativeTime("2026-09-29T11:45:00Z", now), "15 min ago");
  assert.equal(api.relativeTime("2026-09-29T09:00:00Z", now), "3 h ago");
  assert.equal(api.relativeTime("2026-09-27T12:00:00Z", now), "2 d ago");
});

test("relativeTime tolerates a missing or malformed timestamp", () => {
  assert.equal(api.relativeTime(null, Date.now()), "unknown");
  assert.equal(api.relativeTime("not-a-date", Date.now()), "unknown");
});

test("freshnessLine reports data-as-of, checked-when and pending count", () => {
  const now = Date.parse("2026-09-29T12:00:00Z");
  const f = { snapshot_as_of: "2026-09-24", checked_at: "2026-09-29T11:45:00Z", pending_count: 3 };
  assert.equal(api.freshnessLine(f, now), "Data as of 2026-09-24 · checked 15 min ago · 3 filings pending");
});

test("freshnessLine uses singular filing for exactly one pending", () => {
  const now = Date.parse("2026-09-29T12:00:00Z");
  const f = { snapshot_as_of: "2026-09-24", checked_at: "2026-09-29T11:45:00Z", pending_count: 1 };
  assert.match(api.freshnessLine(f, now), /1 filing pending$/);
});

test("freshnessLine says the check is unavailable when nothing has ever run", () => {
  assert.equal(api.freshnessLine({}, Date.now()), "freshness check unavailable");
  assert.equal(api.freshnessLine(null, Date.now()), "freshness check unavailable");
  assert.equal(api.freshnessLine({ status: "unconfigured" }, Date.now()), "freshness check unavailable");
});
