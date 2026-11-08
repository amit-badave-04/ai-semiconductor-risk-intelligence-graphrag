/// <reference types="node" />
import { createRequire } from "node:module";
import path from "node:path";
import { beforeAll, describe, expect, it } from "vitest";
import type { EscalatedEvent, RetrievalEvent } from "../events";
import { costNote, limitsText, retrievalStatus, statusLine } from "../askStatus";

// Transitional (it goes when /legacy goes): the sentences the Ask page computes from data go through the legacy functions (the
// page's own script, loaded by tests/ui/harness.mjs) and through lib/askStatus.ts, and must agree.
const ROOT = path.resolve(import.meta.dirname, "../../../..");

interface Legacy {
  retrievalStatus: (ev: unknown) => string;
  limitsText: (s: unknown) => string;
  costNote: (m: unknown) => string;
}
let legacy: Legacy;
beforeAll(() => {
  const harness = createRequire(import.meta.url)(path.join(ROOT, "tests", "ui", "harness.mjs")) as { loadPage: () => { api: Legacy } };
  legacy = harness.loadPage().api;
});

const MODELS = ["anthropic/claude-sonnet-5", "openai/gpt-6-luna", "x", "", null, undefined, "a/b/c"] as const;
const ANCHORS = [undefined, {}, { Nvidia: 1045810 }, { Nvidia: 1, TSMC: 2 }] as const;
const COUNTS = [undefined, null, {}, { chunks: 8 }, { edges: 2, metrics: 3, risks: 4, temporal: 5, chunks: 6 }, { edges: "2", chunks: 0 }] as const;

describe("lib/askStatus against the legacy functions", () => {
  it("retrievalStatus agrees on every combination of anchors, counts and the default flag", () => {
    for (const anchors of ANCHORS) {
      for (const counts of COUNTS) {
        for (const anchor_defaulted of [undefined, true, false]) {
          const ev = { anchors, counts, anchor_defaulted };
          expect(retrievalStatus(ev as never)).toBe(legacy.retrievalStatus(ev));
        }
      }
    }
    expect(retrievalStatus(null)).toBe(legacy.retrievalStatus(null));
    expect(retrievalStatus(undefined)).toBe(legacy.retrievalStatus(undefined));
  });

  it("costNote agrees on every pair of models", () => {
    for (const llm of MODELS) for (const escalation of MODELS) expect(costNote({ llm, escalation })).toBe(legacy.costNote({ llm, escalation }));
    expect(costNote(undefined)).toBe(legacy.costNote(undefined));
    expect(costNote(null)).toBe(legacy.costNote(null));
  });

  it("limitsText agrees on every combination of limits and models", () => {
    for (const per_ip of [undefined, 0, 5, "10"]) {
      for (const llm of MODELS) {
        for (const escalation of MODELS) {
          const stats = { limits: { per_ip }, models: { llm, escalation } };
          expect(limitsText(stats)).toBe(legacy.limitsText(stats));
        }
      }
    }
    expect(limitsText(null)).toBe(legacy.limitsText(null));
    expect(limitsText({})).toBe(legacy.limitsText({}));
  });
});

describe("statusLine: the progress and the end of an ask, derived from its state", () => {
  const base = { status: "idle", strategy: "", retrieval: null, escalated: null, done: null, statusText: null } as const;

  it("is empty when idle and after an abort (the page has no sentence for a stop)", () => {
    expect(statusLine(base)).toEqual({ text: "", isError: false });
    expect(statusLine({ ...base, status: "aborted" })).toEqual({ text: "", isError: false });
  });

  it("says what is happening while it streams: the agent first, then the retrieval, then the escalation", () => {
    expect(statusLine({ ...base, status: "streaming" }).text).toBe("retrieving from the graph\u2026");
    expect(statusLine({ ...base, status: "streaming", strategy: "agent" }).text).toBe("the research agent is choosing lookups\u2026");
    const retrieval: RetrievalEvent = { event: "retrieval", anchors: { Nvidia: 1 }, counts: { chunks: 2 } };
    expect(statusLine({ ...base, status: "streaming", retrieval }).text).toBe("anchors: Nvidia \u00b7 2 excerpts \u2014 generating\u2026");
    const escalated: EscalatedEvent = { event: "escalated", reasons: [], from: "a", to: "b" };
    expect(statusLine({ ...base, status: "streaming", retrieval, escalated }).text).toMatch(/^the first draft did not pass the automatic checks; answering again/);
  });

  it("a cached answer and a live one end with their own words, and any status text is an error", () => {
    const done = { event: "done" as const, answer: "a", citations: [] as string[], hallucinated: [] as string[] };
    expect(statusLine({ ...base, status: "ended", done: { ...done, cached: true } }).text).toBe("served from cache \u2014 no model call");
    expect(statusLine({ ...base, status: "ended", done: { ...done, cached: false } }).text).toBe("done");
    expect(statusLine({ ...base, status: "failed", statusText: "boom" })).toEqual({ text: "boom", isError: true });
  });
});
