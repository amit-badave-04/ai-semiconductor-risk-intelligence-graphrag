// The upload workspace panel's pure functions (M4 Worker C, docs/v2/M4_PLAN.md 4.2): doc chip labels, the stale
// class, job progress text, a document's version timeline, and the freshness header line. No DOM, no npm packages.
import assert from "node:assert/strict";
import test from "node:test";
import { loadPage, plain } from "./harness.mjs";

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

// ---------------------------------------------------------------- freshnessLine: disabled/never (C3 item 6)

test("freshnessLine says unavailable for disabled and never, the same as unconfigured", () => {
  // routes.status_without_a_monitor / summary_without_a_monitor always pair these statuses with checked_at: null
  // (docs/v2/M4_PLAN.md 15.10) — none of the three needs its own copy here.
  assert.equal(api.freshnessLine({ status: "disabled", checked_at: null, pending_count: 0 }, Date.now()),
    "freshness check unavailable");
  assert.equal(api.freshnessLine({ status: "never", checked_at: null, pending_count: 0 }, Date.now()),
    "freshness check unavailable");
});

// ---------------------------------------------------------------- freshnessLine: finding 25 (M4 review)

test("freshnessLine says the check failed and omits the pending count when status is error", () => {
  const now = Date.parse("2026-09-29T12:00:00Z");
  const f = { status: "error", checked_at: "2026-09-29T11:45:00Z", pending_count: 3, snapshot_as_of: "2026-09-24" };
  assert.equal(api.freshnessLine(f, now), "freshness check failed 15 min ago");
});

test("freshnessLine says the check failed for a stale status too", () => {
  const now = Date.parse("2026-09-29T12:00:00Z");
  const f = { status: "stale", checked_at: "2026-09-27T12:00:00Z", pending_count: 0 };
  assert.equal(api.freshnessLine(f, now), "freshness check failed 2 d ago");
});

// ---------------------------------------------------------------- freshnessLine: ordering bug fix (C3 item 6)

test("freshnessLine says the check failed using last_error_at when no good check has ever landed", () => {
  // The very FIRST check a monitor ever makes can fail before any good check exists: checked_at stays null
  // (monitor._error_result carries an all-empty shape forward), but last_error_at is set. Before the fix, the
  // `!checked_at` branch ran first and this rendered the misleading "freshness check unavailable" instead.
  const now = Date.parse("2026-09-29T12:00:00Z");
  const f = { status: "error", checked_at: null, last_error_at: "2026-09-29T11:45:00Z", pending_count: 0 };
  assert.equal(api.freshnessLine(f, now), "freshness check failed 15 min ago");
});

test("freshnessLine prefers last_error_at over an older checked_at when status is error", () => {
  const now = Date.parse("2026-09-29T12:00:00Z");
  const f = { status: "error", checked_at: "2026-09-20T00:00:00Z", last_error_at: "2026-09-29T11:45:00Z" };
  assert.equal(api.freshnessLine(f, now), "freshness check failed 15 min ago");
});

test("freshnessLine falls back to a bare failed message when neither timestamp is known", () => {
  const f = { status: "error", checked_at: null, last_error_at: null };
  assert.equal(api.freshnessLine(f, Date.now()), "freshness check failed");
});

test("freshnessLine for a stale status ignores last_error_at (there is no failure instant, only an aging good check)", () => {
  const now = Date.parse("2026-09-29T12:00:00Z");
  const f = { status: "stale", checked_at: "2026-09-27T12:00:00Z", last_error_at: "2026-09-01T00:00:00Z" };
  assert.equal(api.freshnessLine(f, now), "freshness check failed 2 d ago");
});

// ---------------------------------------------------------------- uploadTargetOptions / uploadTitleFor: finding 15

test("uploadTargetOptions always offers a new document first", () => {
  const opts = plain(api.uploadTargetOptions([]));
  assert.deepEqual(opts, [{ value: "", label: "Upload a new document" }]);
});

test("uploadTargetOptions offers a 'new version of' entry per existing document, titled or not", () => {
  const docs = [{ document_id: "abc123456789", title: "Q2 board memo.pdf" }, { document_id: "def123456789" }];
  const opts = plain(api.uploadTargetOptions(docs));
  assert.deepEqual(opts, [
    { value: "", label: "Upload a new document" },
    { value: "abc123456789", label: "New version of Q2 board memo.pdf" },
    { value: "def123456789", label: "New version of def123456789" },
  ]);
});

test("uploadTitleFor truncates a long file name to the server's own title cap", () => {
  const long = "a".repeat(200) + ".pdf";
  assert.equal(api.uploadTitleFor(long, 120).length, 120);
  assert.equal(api.uploadTitleFor("short.txt", 120), "short.txt");
  assert.equal(api.uploadTitleFor(null, 120), "");
});

// ---------------------------------------------------------------- versionAskAsOfOptions: section 15.1

test("versionAskAsOfOptions offers 'current' plus one 'ask as of vN' per version, keyed by that version's created_at", () => {
  const versions = [{ version: 1, created_at: "2026-09-01T00:00:00Z" }, { version: 2, created_at: "2026-09-15T00:00:00Z" }];
  const opts = plain(api.versionAskAsOfOptions(versions));
  assert.deepEqual(opts, [
    { value: "", label: "ask as of: current" },
    { value: "2026-09-01T00:00:00Z", label: "ask as of v1" },
    { value: "2026-09-15T00:00:00Z", label: "ask as of v2" },
  ]);
});

test("versionAskAsOfOptions tolerates an empty or missing version list", () => {
  assert.deepEqual(plain(api.versionAskAsOfOptions([])), [{ value: "", label: "ask as of: current" }]);
  assert.deepEqual(plain(api.versionAskAsOfOptions(undefined)), [{ value: "", label: "ask as of: current" }]);
});

// ---------------------------------------------------------------- changeItemHtml / changesHtml: finding 16

test("changeItemHtml shows a headline alone when the unit has no surviving passage", () => {
  assert.equal(api.changeItemHtml({ headline: "Market Outlook" }), "<li>Market Outlook</li>");
});

test("changeItemHtml renders each passage as a quote plus a doc: citation chip", () => {
  const item = { headline: "Market Outlook",
    passages: [{ quote: "shrink next year", chunk_id: "doc:0123456789ab:v2:0003" }] };
  const html = api.changeItemHtml(item);
  assert.match(html, /shrink next year/);
  assert.match(html, /class="cite" data-id="doc:0123456789ab:v2:0003"/);
});

test("changeItemHtml escapes an untrusted headline and quote", () => {
  const item = { headline: "<b>eek</b>", passages: [{ quote: "<script>bad</script>", chunk_id: "doc:0123456789ab:v1:0001" }] };
  const html = api.changeItemHtml(item);
  assert.doesNotMatch(html, /<b>eek<\/b>/);
  assert.doesNotMatch(html, /<script>bad<\/script>/);
});

test("changesHtml lists a minor_rewordings section separately from changed, never folded into unchanged_count", () => {
  const report = { items_compared: true, unchanged_count: 2, added: [], removed: [],
    changed: [{ headline: "Legal Proceedings", passages: [] }],
    minor_rewordings: [{ headline: "Company History" }] };
  const html = api.changesHtml(report, 1, 2);
  assert.match(html, /Minor rewordings \(1\)/);
  assert.match(html, /Company History/);
  assert.match(html, /Changed \(1\)/);
  assert.match(html, /2 section\(s\) unchanged/);
});

test("changesHtml with no minor_rewordings key renders exactly as before (backward compatible)", () => {
  const report = { items_compared: true, unchanged_count: 3, added: [], removed: [], changed: [] };
  const html = api.changesHtml(report, 1, 2);
  assert.doesNotMatch(html, /Minor rewordings/);
});

test("changesHtml says how many sections were compared at section level only when the negation check skipped some", () => {
  // Closing verification: negation_check_skipped was in the API payload but never shown on the page.
  const base = { items_compared: true, unchanged_count: 1, added: [], removed: [], changed: [] };
  assert.match(api.changesHtml({ ...base, negation_check_skipped: 2 }, 1, 2),
    /2 section\(s\) were compared as whole sections, not sentence by sentence for negation changes/);
  assert.doesNotMatch(api.changesHtml({ ...base, negation_check_skipped: 0 }, 1, 2), /sentence by sentence/);
  assert.doesNotMatch(api.changesHtml(base, 1, 2), /sentence by sentence/);
});

test("changesHtml reports the not-compared reason when items_compared is false", () => {
  const html = api.changesHtml({ items_compared: false, not_compared_reason: "identical_content" }, 1, 2);
  assert.match(html, /comparison not available \(identical_content\)/);
});
