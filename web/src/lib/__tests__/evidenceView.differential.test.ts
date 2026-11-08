/// <reference types="node" />
import { createRequire } from "node:module";
import path from "node:path";
import { beforeAll, describe, expect, it } from "vitest";
import { evidenceView, formatNumber } from "../evidenceView";

// Transitional (it goes when /legacy goes): generated payloads of every id form go through the legacy evidenceView (the page's own
// script, loaded by tests/ui/harness.mjs) and through lib/evidenceView.ts, and must agree on the title, the kind, the note, the
// freshness badges and hint, the fact rows, the text and the link.

const ROOT = path.resolve(import.meta.dirname, "../../../..");
const COUNT = 3000;
const IDS = [
  "0001045810-26-000021:I.1A:0361",
  "0000002488-26-000010:II.7:0003",
  "xbrl:1045810:revenue:2026-01-25",
  "xbrl:1045810:research_and_development:2025-01-26",
  "fr:2026-19537",
  "fr:C1-2026-16628",
  "doc:0123456789ab:v2:0007",
  "doc:aaaaaaaaaaaa:v12:0001",
  "junk",
];

interface LegacyView {
  kind: string | null;
  label: string;
  title: string;
  metaHtml: string;
  freshHtml: string;
  factsHtml: string;
  text: string;
  linkHtml: string;
}
interface Legacy {
  evidenceView: (id: string, d: unknown) => LegacyView;
  formatNumber: (n: unknown) => string;
}
let legacy: Legacy;

beforeAll(() => {
  const harness = createRequire(import.meta.url)(path.join(ROOT, "tests", "ui", "harness.mjs")) as { loadPage: () => { api: Legacy } };
  legacy = harness.loadPage().api;
});

const ENTITIES: Record<string, string> = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'" };
const decode = (s: string) => s.replace(/&(?:amp|lt|gt|quot|#39);/g, (e) => ENTITIES[e]);

function readLegacy(v: LegacyView) {
  const facts = [...v.factsHtml.matchAll(/<dt>(.*?)<\/dt><dd>(.*?)<\/dd>/gs)].map((m) => ({ label: decode(m[1]), value: decode(m[2]) }));
  const link = /<a href="(.*?)" target="_blank" rel="noopener">(.*?) \u2197<\/a>/.exec(v.linkHtml);
  const badges = [...v.freshHtml.matchAll(/<span class="badge( (?:ok|warn|bad))?">(.*?)<\/span>/g)].map((m) => ({ text: decode(m[2]), cls: (m[1] ?? "").trim() }));
  const hint = /<div class="hint">(.*?)<\/div>/.exec(v.freshHtml)?.[1] ?? "";
  const note = decode(v.metaHtml.slice(0, v.metaHtml.lastIndexOf("<div>id: ")));
  return { kind: v.kind, label: v.label, title: v.title, note, fresh: { badges, hint: decode(hint) }, facts, text: v.text, link: link ? { href: decode(link[1]), label: decode(link[2]) } : null };
}

/** The view with its fields in one fixed order, so two equal views stringify equally (the shipped view also carries the id). */
function ordered(v: { kind: unknown; label: unknown; title: unknown; note: unknown; fresh: unknown; facts: unknown; text: unknown; link: unknown }): string {
  return JSON.stringify([v.kind, v.label, v.title, v.note, v.fresh, v.facts, v.text, v.link]);
}

function rng(seed: number): () => number {
  let a = seed;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function payloads(count: number): { id: string; d: unknown }[] {
  const r = rng(20261108);
  const pick = <T,>(xs: readonly T[]): T => xs[Math.floor(r() * xs.length)];
  const some = (name: string, xs: readonly unknown[]) => (r() < 0.6 ? { [name]: pick(xs) } : {});
  const url = ["https://www.sec.gov/Archives/x.htm", "https://www.federalregister.gov/d/2026-19537", ["java", "script:alert(1)"].join(""), "http://x.test", "", null] as const;
  return Array.from({ length: count }, () => {
    const id = pick(IDS);
    const d = pick([null, "x", 5, undefined] as const);
    if (r() < 0.05) return { id, d };
    return {
      id,
      d: {
        ...some("filer", ["NVIDIA Corp", "<b>x</b>", "", null]), ...some("form", ["10-K", "20-F", "", null]), ...some("filing_date", ["2026-02-25", "", null]),
        ...some("section_title", ["Risk Factors", "", null]), ...some("mentions", [["TSMC", "Micron"], [], "x", null]),
        ...some("status", ["current", "superseded", "corrected", "weird", "", null]), ...some("is_current", [true, false, null]),
        ...some("retrievable", [true, false, null]), ...some("valid_to", ["2026-02-24", "", null]), ...some("superseded_by", ["0001-27", "", null]),
        ...some("corrected_by", ["0000-26", "", null]), ...some("superseded_by_version", [2, 3, null]), ...some("item_headlines", [["A", "", "B"], [], "no", null]),
        ...some("text", ["We are <b>subject</b>", "", 5, null]), ...some("source_url", url), ...some("url", url),
        ...some("title", ["Rule title", "My doc", "", null]), ...some("company", ["NVIDIA Corp", "", null]), ...some("metric", ["revenue", "", null]),
        ...some("concept", ["us-gaap:Revenues", "", null]), ...some("value", [215938000000, "1234.5", "abc", 0, "", null]), ...some("unit", ["USD", "", null]),
        ...some("period", ["2025-01-27 to 2026-01-25", { start: "a", end: "b" }, { start: "a" }, "", null]), ...some("period_start", ["2025-01-27", "", null]),
        ...some("period_end", ["2026-01-25", "", null]), ...some("accession_no", ["0001045810-26-000021", "", null]), ...some("accession", ["0001-26", "", null]),
        ...some("publication_date", ["2026-09-10", "", null]), ...some("document_number", ["2026-19537", "", null]), ...some("kind", ["licensing_policy", "", null]),
        ...some("topics", [["china", "hbm"], [], "x", null]), ...some("relevant", [true, false, null]), ...some("abstract", ["The abstract.", "", 5, null]),
      },
    };
  });
}

describe("lib/evidenceView against the legacy evidenceView", () => {
  it("3000 generated payloads give the same view", () => {
    const bad: string[] = [];
    for (const { id, d } of payloads(COUNT)) {
      const mine = ordered(evidenceView(id, d));
      const old = ordered(readLegacy(legacy.evidenceView(id, d)));
      if (mine !== old) bad.push(`${id} ${JSON.stringify(d).slice(0, 100)} => ${mine.slice(0, 200)} VS ${old.slice(0, 200)}`);
    }
    expect(bad.slice(0, 3)).toEqual([]);
  });

  it("formatNumber agrees on a set of values", () => {
    for (const v of [0, 1, 1234.56789, -5, "7", "1e3", " ", "", "abc", null, undefined, NaN, Infinity, 215938000000, true, {}]) {
      expect(formatNumber(v)).toBe(legacy.formatNumber(v));
    }
  });

  it("the corpus reaches every kind and every freshness badge (the comparison is not vacuous)", () => {
    const all = payloads(COUNT).map(({ id, d }) => JSON.stringify(evidenceView(id, d))).join("");
    for (const marker of ['"kind":"chunk"', '"kind":"xbrl"', '"kind":"fr"', '"kind":"doc"', '"kind":null', "superseded by", "corrected by", '"cls":"ok"', "valid until", "href"]) expect(all).toContain(marker);
  });
});
