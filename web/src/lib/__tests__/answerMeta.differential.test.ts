/// <reference types="node" />
import { readFileSync, readdirSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { beforeAll, describe, expect, it } from "vitest";
import { answerBadge, checksPassed, checksSummary, escalationStatus, metaSummary, reasonText } from "../answerMeta";

// Transitional (it goes when /legacy goes): the badges and checks of every recorded `done` event and of a seeded corpus of
// generated events go through the legacy functions (the page's own script, loaded by tests/ui/harness.mjs) and through
// lib/answerMeta.ts, and must agree: the same badges (text and class), warnings and hint, and the same footer as legacy metaHtml.

const ROOT = path.resolve(import.meta.dirname, "../../../..");
const SSE_DIR = path.join(ROOT, "tests", "data", "sse");
const GENERATED = 3000;

interface Legacy {
  answerBadge: (ev: unknown, model?: unknown) => unknown;
  checksSummary: (ev: unknown) => unknown;
  checksPassed: (c: unknown) => boolean;
  reasonText: (r: unknown) => string;
  escalationStatus: (ev: unknown) => string;
  metaHtml: (ev: unknown, model?: unknown) => string;
}
let legacy: Legacy;

beforeAll(() => {
  const harness = createRequire(import.meta.url)(path.join(ROOT, "tests", "ui", "harness.mjs")) as { loadPage: () => { api: Legacy } };
  legacy = harness.loadPage().api;
});

const plain = (value: unknown): unknown => JSON.parse(JSON.stringify(value ?? null));

const ENTITIES: Record<string, string> = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'" };
const decode = (s: string) => s.replace(/&(?:amp|lt|gt|quot|#39);/g, (e) => ENTITIES[e]);

/** The footer the legacy metaHtml wrote, read back as badges, warnings and hint. */
function readLegacyFooter(html: string) {
  const badges = [...html.matchAll(/<span class="badge( (?:ok|warn|bad))?">(.*?)<\/span>/g)].map((m) => ({ text: decode(m[2]), cls: (m[1] ?? "").trim() }));
  const warnings = [...html.matchAll(/<li>(.*?)<\/li>/g)].map((m) => decode(m[1]));
  const hint = /<div class="hint" style="margin-top:6px">(.*)<\/div>$/.exec(html)?.[1] ?? "";
  return { badges, warnings, hint: decode(hint) };
}

function recordedDone(): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = [];
  for (const name of readdirSync(SSE_DIR).filter((n) => n.endsWith(".expected.json"))) {
    const events = (JSON.parse(readFileSync(path.join(SSE_DIR, name), "utf-8")) as { events: Record<string, unknown>[] }).events;
    for (const e of events) if (e.event === "done") out.push(e);
  }
  return out;
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

function generate(count: number): Record<string, unknown>[] {
  const r = rng(20261108);
  const pick = <T,>(xs: readonly T[]): T => xs[Math.floor(r() * xs.length)];
  const bool = () => pick([true, false, undefined] as const);
  const strs = () => pick([[], ["$1.5B"], ["$9.9B", "43%"], ["<b>x</b>"], "nope", null] as const);
  return Array.from({ length: count }, () => {
    const checks = pick([undefined, null, "x", {}, {
      citations_retrieved: bool(), numbers_grounded: bool(), numbers_checked: pick([undefined, 0, 3, "2"] as const),
      unmatched_numbers: strs(), echoed_numbers: strs(), pseudo_citations: strs(), has_citation: bool(), is_refusal: bool(),
      unsupported_removal_claim: bool(), unsupported_removal_sentences: pick([[], ["It dropped X."], "no"] as const),
    }] as const);
    return {
      cached: pick([true, false, undefined] as const), source: pick(["benchmark", "x", undefined] as const),
      answered_by: pick([undefined, null, "anthropic/claude-sonnet-5", "openai/gpt-6-luna", "x/<img>"] as const),
      routed: pick([undefined, null, "cheap", "strong"] as const), escalated: bool(),
      escalation_reasons: pick([undefined, [], ["ungrounded_number", "invalid_citation"], ["some_new_reason"], "x"] as const),
      finish_reason: pick([undefined, null, "length", "stop"] as const),
      usage: pick([undefined, null, {}, { prompt_tokens: 12, completion_tokens: 34 }, { prompt_tokens: 0, completion_tokens: 5, estimated: true }] as const),
      cost_usd: pick([undefined, null, 0, 0.0123, "free"] as const),
      citations: pick([undefined, [], ["a"], ["a", "b"], "nope", null] as const),
      hallucinated: pick([undefined, [], ["b"], null] as const),
      checks,
    };
  });
}

const MODELS = [undefined, "", "anthropic/claude-sonnet-5"] as const;

function differences(ev: Record<string, unknown>): string[] {
  const found: string[] = [];
  const same = (what: string, a: unknown, b: unknown) => {
    if (JSON.stringify(plain(a)) !== JSON.stringify(plain(b))) found.push(`${what} of ${JSON.stringify(ev).slice(0, 120)}`);
  };
  same("checksSummary", checksSummary(ev), legacy.checksSummary(ev));
  same("checksPassed", checksPassed(ev.checks), legacy.checksPassed(ev.checks));
  for (const model of MODELS) {
    same(`answerBadge(${String(model)})`, answerBadge(ev, model), legacy.answerBadge(ev, model));
    same(`footer(${String(model)})`, metaSummary(ev, model), readLegacyFooter(legacy.metaHtml(ev, model)));
  }
  for (const reason of list(ev.escalation_reasons)) same("reasonText", reasonText(reason), legacy.reasonText(reason));
  same("escalationStatus", escalationStatus({ reasons: ev.escalation_reasons }), legacy.escalationStatus({ reasons: ev.escalation_reasons }));
  return found;
}
const list = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);

describe("lib/answerMeta against the legacy functions", () => {
  it("every recorded done event gives the same badges, warnings, hint and footer", () => {
    const events = recordedDone();
    expect(events.length).toBeGreaterThanOrEqual(6);
    expect(events.flatMap(differences)).toEqual([]);
  });

  it("3000 generated events give the same badges, warnings, hint and footer", () => {
    expect(generate(GENERATED).flatMap(differences).slice(0, 5)).toEqual([]);
  });

  it("the corpus reaches every badge colour and warnings (the comparison is not vacuous)", () => {
    const all = generate(GENERATED).map((ev) => JSON.stringify(checksSummary(ev))).join("");
    for (const marker of ['"cls":"ok"', '"cls":"warn"', '"cls":"bad"', "Numbers not found", "cites no source", "removal"]) expect(all).toContain(marker);
  });
});
