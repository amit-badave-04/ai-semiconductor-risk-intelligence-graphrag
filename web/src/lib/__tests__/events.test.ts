import { describe, expect, it } from "vitest";
import {
  decodeEvent,
  decodeJobEvent,
  isCachedDoneEvent,
  isDeltaEvent,
  isDoneEvent,
  isErrorEvent,
  isEscalatedEvent,
  isJobEvent,
  isLiveDoneEvent,
  isRetrievalEvent,
  isStepEvent,
  isTerminal,
} from "../events";
import { ASK_FIXTURES, fixtureEvents, JOB_FIXTURES, preM5Events } from "./fixtures";

// R17 (docs/specs/m5b-frontend.md): the guards accept every recorded event and reject a malformed one (S6).
const decode = (event: Record<string, unknown>) => decodeEvent(String(event.event), JSON.stringify(event));

describe("decodeEvent over every recorded event (the Python sidecars)", () => {
  it.each(ASK_FIXTURES)("%s: each event decodes to exactly what the server yielded", (name) => {
    for (const event of fixtureEvents(name)) {
      expect(decode(event)).toEqual(event);
    }
  });

  it.each(JOB_FIXTURES)("%s: each job event is a valid job event", (name) => {
    for (const event of fixtureEvents(name)) {
      expect(decodeJobEvent("job", JSON.stringify(event))).toEqual(event);
      expect(isJobEvent(event)).toBe(true);
    }
  });

  it("tells the cached done from the live done", () => {
    const [cached] = fixtureEvents("cached_done");
    const live = fixtureEvents("live_hybrid").at(-1);
    expect(isCachedDoneEvent(cached) && !isLiveDoneEvent(cached)).toBe(true);
    expect(isLiveDoneEvent(live) && !isCachedDoneEvent(live)).toBe(true);
    expect(isDoneEvent(cached) && isDoneEvent(live)).toBe(true);
  });

  it("types each kind", () => {
    const kinds = fixtureEvents("agent_steps").map((e) => e.event);
    expect(new Set(kinds)).toEqual(new Set(["step", "retrieval", "delta", "done"]));
    expect(fixtureEvents("escalated").some((e) => isEscalatedEvent(e))).toBe(true);
    expect(isErrorEvent(fixtureEvents("error_before_delta").at(-1))).toBe(true);
    const error = decode(fixtureEvents("error_before_delta").at(-1) ?? {});
    const delta = decode(fixtureEvents("live_hybrid")[1] ?? {});
    expect(error !== null && isTerminal(error)).toBe(true);
    expect(delta !== null && isTerminal(delta)).toBe(false);
  });
});

describe("events the server really emits, from the pre-M5 recordings of the agent and workspace paths", () => {
  const recorded = preM5Events();

  it("covers every scenario the recordings name", () => {
    expect(recorded.length).toBeGreaterThanOrEqual(11);
  });

  it.each(recorded.map((r) => [r.name, r.events] as const))("%s: every event decodes", (_name, events) => {
    for (const event of events) {
      expect(decode(event), JSON.stringify(event).slice(0, 80)).toEqual(event);
    }
  });

  it("accepts a done whose usage and cost the provider did not report (null)", () => {
    const base = { answer: "a", citations: [], hallucinated: [] };
    expect(isLiveDoneEvent({ ...base, usage: null, cost_usd: null })).toBe(true);
  });

  it("accepts the job event of a first version: items_compared is a boolean", () => {
    const ready = { job_id: "j", state: "ready", document_id: "d", version: 1, items_compared: false, not_compared_reason: "first_version" };
    expect(isJobEvent(ready)).toBe(true);
  });
});

describe("malformed frames are rejected, never cast (S6)", () => {
  const done = fixtureEvents("agent_steps").at(-1) ?? {};
  const agent = done.agent as Record<string, unknown>;

  it("rejects a string elapsed_s (the value the pre-M5 recorder used to write)", () => {
    expect(isLiveDoneEvent({ ...done, agent: { ...agent, elapsed_s: "<elapsed_s>" } })).toBe(false);
    expect(decode({ ...done, agent: { ...agent, elapsed_s: "1.5" } })).toBeNull();
  });

  it("rejects non-finite numbers, which JSON cannot carry but a guard must not trust", () => {
    expect(isLiveDoneEvent({ ...done, cost_usd: Number.NaN })).toBe(false);
    expect(isLiveDoneEvent({ ...done, agent: { ...agent, elapsed_s: Number.POSITIVE_INFINITY } })).toBe(false);
    expect(isStepEvent({ event: "step", n: Number.NaN, tool: "t", summary: "s", ok: true })).toBe(false);
    expect(isRetrievalEvent({ anchors: { Nvidia: Number.NaN }, counts: {} })).toBe(false);
  });

  it.each([
    ["retrieval", { anchors: [], counts: {} }],
    ["retrieval", { anchors: { Nvidia: "1" }, counts: {} }],
    ["step", { n: "1", tool: "t", summary: "s", ok: true }],
    ["step", { n: 1, tool: "t", summary: "s", ok: "yes" }],
    ["delta", { text: 5 }],
    ["escalated", { reasons: "x", from: "a", to: "b" }],
    ["done", { answer: 1, citations: [], hallucinated: [] }],
    ["done", { answer: "a", citations: [1], hallucinated: [] }],
    ["done", { answer: "a", citations: [], hallucinated: [], usage: { prompt_tokens: "1", completion_tokens: 1 } }],
    ["done", { answer: "a", citations: [], hallucinated: [], cached: "yes" }],
    ["done", { answer: "a", citations: [], hallucinated: [], workspace: { id_hash: 1 } }],
    ["done", { answer: "a", citations: [], hallucinated: [], checks: [] }],
    ["error", { detail: 5 }],
  ])("rejects a %s with the wrong field types: %j", (name, data) => {
    expect(decodeEvent(name, JSON.stringify(data))).toBeNull();
  });

  it("rejects non-JSON data, arrays, null and an unknown event name", () => {
    expect(decodeEvent("delta", "not json")).toBeNull();
    expect(decodeEvent("delta", "[]")).toBeNull();
    expect(decodeEvent("delta", "null")).toBeNull();
    expect(decodeEvent("surprise", '{"text":"x"}')).toBeNull();
    expect(decodeEvent(undefined, '{"text":"x"}')).toBeNull();
    expect(decodeEvent("ping", "{}")).toBeNull();
  });

  it("ignores unknown fields", () => {
    expect(decodeEvent("delta", '{"text":"x","extra":{"a":1}}')).toEqual({ event: "delta", text: "x", extra: { a: 1 } });
  });

  it("keeps an optional field optional but typed", () => {
    expect(isCachedDoneEvent({ answer: "a", citations: [], hallucinated: [], cached: true, source: 3 })).toBe(false);
    expect(isCachedDoneEvent({ answer: "a", citations: [], hallucinated: [], cached: true })).toBe(true);
    expect(isLiveDoneEvent({ answer: "a", citations: [], hallucinated: [], cached: false, finish_reason: null })).toBe(true);
    expect(isLiveDoneEvent({ answer: "a", citations: [], hallucinated: [], agent: { elapsed_s: 1, model_calls: "2" } })).toBe(false);
    expect(isLiveDoneEvent({ answer: "a", citations: [], hallucinated: [], agent: { elapsed_s: 1, fallback_reason: 3 } })).toBe(false);
    expect(isDeltaEvent({ text: "" })).toBe(true);
  });

  it("types the routing fields of a live done (answered_by, routed, escalated, escalation_reasons): optional, wrong types rejected", () => {
    const base = { answer: "a", citations: [], hallucinated: [] };
    const escalated = fixtureEvents("escalated").at(-1) ?? {};
    expect(isLiveDoneEvent(escalated)).toBe(true);
    expect(isLiveDoneEvent(base)).toBe(true);
    expect(isLiveDoneEvent({ ...base, answered_by: "m", routed: "strong", escalated: false })).toBe(true);
    expect(isLiveDoneEvent({ ...base, escalated: true, escalation_reasons: ["a", "b"] })).toBe(true);
    for (const bad of [
      { answered_by: 5 },
      { routed: 1 },
      { escalated: "yes" },
      { escalation_reasons: "x" },
      { escalation_reasons: [1] },
    ]) {
      expect(isLiveDoneEvent({ ...base, ...bad }), JSON.stringify(bad)).toBe(false);
      expect(decodeEvent("done", JSON.stringify({ ...base, ...bad })), JSON.stringify(bad)).toBeNull();
    }
  });

  it("types retrieval.anchor_defaulted: optional boolean, a wrong type rejects the event", () => {
    const base = { anchors: {}, counts: {} };
    expect(isRetrievalEvent(base)).toBe(true);
    expect(isRetrievalEvent({ ...base, anchor_defaulted: true })).toBe(true);
    expect(isRetrievalEvent({ ...base, anchor_defaulted: "true" })).toBe(false);
    expect(decodeEvent("retrieval", JSON.stringify({ ...base, anchor_defaulted: 1 }))).toBeNull();
  });

  it.each([
    { job_id: 1, state: "ready", document_id: "d", version: 1 },
    { job_id: "j", state: "done", document_id: "d", version: 1 },
    { job_id: "j", state: "ready", document_id: "d", version: "1" },
    { job_id: "j", state: "embedding", document_id: "d", version: 1, progress: { done: "1", total: 2, eta_s: null } },
    { job_id: "j", state: "embedding", document_id: "d", version: 1, progress: { done: 1, total: 2, eta_s: "soon" } },
    { job_id: "j", state: "failed", document_id: "d", version: 1, error: { code: "x" } },
    { job_id: "j", state: "ready", document_id: "d", version: 1, chunks: "12" },
    { job_id: "j", state: "ready", document_id: "d", version: 1, items_compared: 0 },
    { job_id: "j", state: "ready", document_id: "d", version: 1, suspicious: "no" },
  ])("rejects a malformed job event %j", (data) => {
    expect(isJobEvent(data)).toBe(false);
    expect(decodeJobEvent("job", JSON.stringify(data))).toBeNull();
  });

  it("decodes a job event only under the job name and only from JSON", () => {
    const valid = { job_id: "j", state: "received", document_id: "d", version: null };
    expect(decodeJobEvent("delta", JSON.stringify(valid))).toBeNull();
    expect(decodeJobEvent("job", "nope")).toBeNull();
    expect(decodeJobEvent("job", JSON.stringify(valid))).toEqual(valid);
  });
});
