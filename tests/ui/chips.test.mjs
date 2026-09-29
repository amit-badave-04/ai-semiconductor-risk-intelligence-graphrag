// Citation chips: readable labels instead of bare four-digit numbers, one grammar shared with retrieval/ids.py.
import assert from "node:assert/strict";
import test from "node:test";
import { loadPage } from "./harness.mjs";

const { api } = loadPage();
const CHUNK = "0001045810-26-000021:I.1A:0361";
const XBRL = "xbrl:1045810:revenue:2026-01-25";
const FR = "fr:2026-19537";

test("citation ids are classified with the three-form grammar", () => {
  assert.equal(api.classifyCitation(CHUNK), "chunk");
  assert.equal(api.classifyCitation(XBRL), "xbrl");
  assert.equal(api.classifyCitation(FR), "fr");
  assert.equal(api.classifyCitation("fr:C1-2026-16628"), "fr");
  assert.equal(api.classifyCitation("Reported Metrics"), null);
  assert.equal(api.classifyCitation("0361"), null);
  assert.equal(api.classifyCitation(undefined), null);
});

test("an uploaded-document id is the fourth form and gets a readable chip", () => {
  const DOC = "doc:0123456789ab:v2:0007";
  assert.equal(api.classifyCitation(DOC), "doc");
  assert.equal(api.classifyCitation("doc:0123456789AB:v2:0007"), null);
  assert.equal(api.chipLabel(DOC), "your document v2 ¶0007");
  assert.match(api.renderMarkdown(`It says so [${DOC}].`), new RegExp(`<span class="cite" data-id="${DOC}"[^>]*>your document v2 ¶0007</span>`));
});

test("a chunk chip shows the item and paragraph, not a bare number", () => {
  assert.equal(api.chipLabel(CHUNK), "Item 1A ¶0361");
  assert.equal(api.chipLabel("0000002488-26-000021:II.7:0012"), "Item 7 ¶0012");
});

test("once evidence has loaded a chunk chip also shows the form and period", () => {
  assert.equal(api.chipLabel(CHUNK, { form: "10-K", filing_date: "2026-02-25" }), "10-K filed 2026-02-25 · Item 1A ¶0361");
  assert.equal(api.chipLabel(CHUNK, { form: "10-K", fiscal_year: 2026, filing_date: "2026-02-25" }), "10-K FY2026 · Item 1A ¶0361");
  assert.equal(api.chipLabel(CHUNK, {}), "Item 1A ¶0361");
  assert.equal(api.chipLabel(CHUNK, null), "Item 1A ¶0361");
});

test("XBRL and Federal Register chips are readable", () => {
  assert.equal(api.chipLabel(XBRL), "XBRL revenue FY ended 2026-01-25");
  assert.equal(api.chipLabel("xbrl:1045810:research_and_development:2026-01-25"), "XBRL research and development FY ended 2026-01-25");
  assert.equal(api.chipLabel(FR), "BIS rule 2026-19537");
  assert.equal(api.chipLabel("fr:C1-2026-16628"), "BIS rule C1-2026-16628");
});

test("an id outside the grammar is shown as it is", () => {
  assert.equal(api.chipLabel("weird"), "weird");
  assert.equal(api.chipLabel(null), "");
});

test("renderMarkdown turns each of the three id forms into a labelled chip", () => {
  const html = api.renderMarkdown(`Revenue was $215.9B [${XBRL}], see risk [${CHUNK}] and the rule [${FR}].`);
  assert.match(html, new RegExp(`<span class="cite" data-id="${CHUNK}"[^>]*>Item 1A ¶0361</span>`));
  assert.match(html, /data-id="xbrl:1045810:revenue:2026-01-25"[^>]*>XBRL revenue FY ended 2026-01-25</);
  assert.match(html, /data-id="fr:2026-19537"[^>]*>BIS rule 2026-19537</);
});

test("bracketed text that is not a citation stays plain text", () => {
  const html = api.renderMarkdown("Growth [Reported Metrics] and [0001045810-26-000021 lineage data].");
  assert.doesNotMatch(html, /class="cite"/);
  assert.match(html, /\[Reported Metrics\]/);
});

test("two ids in one bracket are not a citation (one per bracket)", () => {
  const html = api.renderMarkdown(`Both [${CHUNK}, ${XBRL}].`);
  assert.doesNotMatch(html, /class="cite"/);
});

test("model text cannot inject markup through the answer renderer", () => {
  const html = api.renderMarkdown('<script>alert(1)</script> <img src=x onerror="alert(2)"> [' + CHUNK + "]");
  assert.doesNotMatch(html, /<script/i);
  assert.doesNotMatch(html, /<img/i);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /class="cite"/);
});

test("bold, bullet lists and headings still render", () => {
  const html = api.renderMarkdown("## Title\n\n- one **bold** item\n- two\n\nPlain.");
  assert.match(html, /<h3>Title<\/h3>/);
  assert.match(html, /<ul><li>one <b>bold<\/b> item<\/li><li>two<\/li><\/ul>/);
  assert.match(html, /<p>Plain\.<\/p>/);
});

test("esc handles non-strings and quotes", () => {
  assert.equal(api.esc(null), "");
  assert.equal(api.esc(42), "42");
  assert.equal(api.esc(`<a href="x">'&'</a>`), "&lt;a href=&quot;x&quot;&gt;&#39;&amp;&#39;&lt;/a&gt;");
});
