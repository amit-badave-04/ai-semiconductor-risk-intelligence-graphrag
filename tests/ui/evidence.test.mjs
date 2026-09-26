// The evidence drawer: freshness fields for filing excerpts, payloads for xbrl and fr ids, and XSS safety.
import assert from "node:assert/strict";
import test from "node:test";
import { loadPage, plain } from "./harness.mjs";

const { api } = loadPage();
const CHUNK = "0001045810-26-000021:I.1A:0361";
const XBRL = "xbrl:1045810:revenue:2026-01-25";
const FR = "fr:2026-19537";

const CHUNK_PAYLOAD = {
  chunk_id: CHUNK, text: "We are subject to laws on privacy <b>fines</b>.", source_url: "https://www.sec.gov/Archives/x.htm",
  section_key: "0001045810-26-000021:I.1A", section_title: "Risk Factors", accession_no: "0001045810-26-000021",
  form: "10-K", filing_date: "2026-02-25", filer: "NVIDIA Corp", mentions: ["TSMC", "Micron"],
  status: "current", is_current: true, retrievable: true, valid_to: null, superseded_by: null, corrected_by: null,
};

test("a current filing excerpt shows a current badge, its provenance and a safe link", () => {
  const v = plain(api.evidenceView(CHUNK, CHUNK_PAYLOAD));
  assert.equal(v.kind, "chunk");
  assert.match(v.title, /10-K filed 2026-02-25 · Item 1A ¶0361/);
  assert.match(v.metaHtml, /NVIDIA Corp/);
  assert.match(v.metaHtml, /Risk Factors/);
  assert.match(v.metaHtml, /mentions: TSMC, Micron/);
  assert.match(v.freshHtml, /badge ok">current</);
  assert.equal(v.text, CHUNK_PAYLOAD.text, "the excerpt is returned as raw text and shown through textContent");
  assert.match(v.linkHtml, /href="https:\/\/www\.sec\.gov\/Archives\/x\.htm"/);
  assert.match(v.linkHtml, /rel="noopener"/);
});

test("a superseded excerpt names the replacing filing and the end of validity", () => {
  const v = plain(api.evidenceView(CHUNK, {
    ...CHUNK_PAYLOAD, status: "superseded", is_current: false, retrievable: false,
    valid_to: "2026-02-24", superseded_by: "0001045810-27-000010",
  }));
  assert.match(v.freshHtml, /superseded by 0001045810-27-000010/);
  assert.match(v.freshHtml, /valid until 2026-02-24/);
  assert.match(v.freshHtml, /not returned by default search/);
  assert.doesNotMatch(v.freshHtml, /badge ok/);
});

test("a corrected excerpt names the amending filing", () => {
  const v = plain(api.evidenceView("0000002488-26-000010:II.7:0003", {
    ...CHUNK_PAYLOAD, status: "corrected", is_current: false, retrievable: false, corrected_by: "0000002488-26-000021",
  }));
  assert.match(v.freshHtml, /corrected by 0000002488-26-000021/);
  assert.match(v.freshHtml, /badge bad/);
});

test("a risk item headline, when present, is shown", () => {
  const v = plain(api.evidenceView(CHUNK, { ...CHUNK_PAYLOAD, headline: "We may be subject to fines under privacy laws" }));
  assert.match(v.factsHtml, /We may be subject to fines under privacy laws/);
});

test("an XBRL id shows the metric payload", () => {
  const v = plain(api.evidenceView(XBRL, {
    value: 215938000000, unit: "USD", period: "2025-01-27 to 2026-01-25", concept: "us-gaap:Revenues",
    accession: "0001045810-26-000021", company: "NVIDIA Corp",
  }));
  assert.equal(v.kind, "xbrl");
  assert.match(v.title, /XBRL revenue FY ended 2026-01-25/);
  assert.match(v.factsHtml, /NVIDIA Corp/);
  assert.match(v.factsHtml, /215,938,000,000 USD/);
  assert.match(v.factsHtml, /us-gaap:Revenues/);
  assert.match(v.factsHtml, /2025-01-27 to 2026-01-25/);
  assert.match(v.factsHtml, /0001045810-26-000021/);
  assert.equal(v.text, "");
});

test("the payload shapes /api/evidence returns are rendered (routes.py XBRL, Federal Register and chunk queries)", () => {
  const xbrl = plain(api.evidenceView(XBRL, {
    type: "xbrl", metric_id: "1045810:revenue:2026-01-25", metric: "revenue", concept: "us-gaap:Revenues",
    value: 215938000000, unit: "USD", period_start: "2025-01-27", period_end: "2026-01-25", company: "NVIDIA Corp",
    cik: 1045810, accession_no: "0001045810-26-000021", form: "10-K", filing_date: "2026-02-25",
    source_url: "https://www.sec.gov/Archives/edgar/data/1045810/x.htm",
  }));
  assert.match(xbrl.factsHtml, /215,938,000,000 USD/);
  assert.match(xbrl.factsHtml, /2025-01-27 to 2026-01-25/);
  assert.match(xbrl.factsHtml, /10-K · filed 2026-02-25 · 0001045810-26-000021/);
  assert.match(xbrl.linkHtml, /sec\.gov/);
  const fr = plain(api.evidenceView(FR, {
    type: "fr", source: "federal_register", external: true, note: "A Federal Register rule linked to companies by keyword match; not a company disclosure.",
    document_number: "2026-19537", title: "Rule title", publication_date: "2026-09-10", url: "https://www.federalregister.gov/d/2026-19537",
    kind: "entity_list_additions", topics: ["china", "hbm"], relevant: false, abstract: "The abstract of the rule.",
  }));
  assert.match(fr.factsHtml, /china, hbm/);
  assert.match(fr.factsHtml, /flagged relevant<\/dt><dd>no/);
  assert.equal(fr.text, "The abstract of the rule.");
  assert.match(fr.metaHtml, /external event/i);
  const chunk = plain(api.evidenceView(CHUNK, { ...CHUNK_PAYLOAD, item_headlines: ["We may face privacy fines"] }));
  assert.match(chunk.factsHtml, /We may face privacy fines/);
});

test("an XBRL payload with a period object still renders", () => {
  const v = plain(api.evidenceView(XBRL, { value: 1, unit: "USD", period: { start: "2025-01-27", end: "2026-01-25" } }));
  assert.match(v.factsHtml, /2025-01-27 to 2026-01-25/);
});

test("a Federal Register id shows the rule and says it is an external keyword match", () => {
  const v = plain(api.evidenceView(FR, {
    title: "Revision to license exceptions", publication_date: "2026-09-10", document_number: "2026-19537",
    url: "https://www.federalregister.gov/d/2026-19537", kind: "licensing_policy", relevant: true,
  }));
  assert.equal(v.kind, "fr");
  assert.match(v.title, /BIS rule 2026-19537/);
  assert.match(v.factsHtml, /Revision to license exceptions/);
  assert.match(v.factsHtml, /2026-09-10/);
  assert.match(v.factsHtml, /licensing_policy/);
  assert.match(v.metaHtml, /external event/i);
  assert.match(v.metaHtml, /keyword/i);
  assert.match(v.metaHtml, /not a statement (made )?by the company/i);
  assert.match(v.linkHtml, /href="https:\/\/www\.federalregister\.gov\/d\/2026-19537"/);
});

test("only https links are ever rendered", () => {
  assert.equal(api.safeUrl("https://www.sec.gov/a?b=1&c=2"), "https://www.sec.gov/a?b=1&c=2");
  assert.equal(api.safeUrl("javascript:alert(1)"), "");
  assert.equal(api.safeUrl("http://example.com"), "");
  assert.equal(api.safeUrl("data:text/html,<script>"), "");
  assert.equal(api.safeUrl(undefined), "");
  const v = plain(api.evidenceView(FR, { title: "x", url: "javascript:alert(1)" }));
  assert.equal(v.linkHtml, "");
});

test("every server-supplied field is escaped in the drawer markup", () => {
  const evil = '<img src=x onerror=alert(1)>"><script>alert(2)</script>';
  const chunk = plain(api.evidenceView(CHUNK, {
    ...CHUNK_PAYLOAD, filer: evil, section_title: evil, form: evil, mentions: [evil], status: evil,
    superseded_by: evil, corrected_by: evil, valid_to: evil, headline: evil, source_url: 'https://x.test/"><script>1</script>',
  }));
  const xbrl = plain(api.evidenceView(XBRL, { value: evil, unit: evil, period: evil, concept: evil, accession: evil, company: evil }));
  const fr = plain(api.evidenceView(FR, { title: evil, publication_date: evil, document_number: evil, kind: evil, url: `https://x.test/${evil}` }));
  for (const v of [chunk, xbrl, fr]) {
    // `title` and `text` are assigned through textContent by the page, so they are plain text, not markup.
    for (const html of [v.metaHtml, v.freshHtml, v.factsHtml, v.linkHtml]) {
      assert.doesNotMatch(html, /<script/i);
      assert.doesNotMatch(html, /<img/i);
      assert.doesNotMatch(html, /onerror=alert\(1\)>/);
    }
  }
});

test("an empty or partial payload never throws", () => {
  assert.doesNotThrow(() => api.evidenceView(CHUNK, {}));
  assert.doesNotThrow(() => api.evidenceView(XBRL, {}));
  assert.doesNotThrow(() => api.evidenceView(FR, {}));
  assert.doesNotThrow(() => api.evidenceView("junk", null));
  assert.equal(plain(api.evidenceView(CHUNK, {})).text, "");
});

test("formatNumber groups digits and leaves non-numbers alone", () => {
  assert.equal(api.formatNumber(215938000000), "215,938,000,000");
  assert.equal(api.formatNumber("1234.5"), "1,234.5");
  assert.equal(api.formatNumber("abc"), "abc");
  assert.equal(api.formatNumber(null), "");
});
