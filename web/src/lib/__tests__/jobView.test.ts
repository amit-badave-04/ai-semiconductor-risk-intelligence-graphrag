import { describe, expect, it } from "vitest";
import type { JobEvent } from "../events";
import { ANNOUNCE_START, announceStep, foldJob, type JobView } from "../jobView";
import { fixtureEvents } from "./fixtures";

const events = fixtureEvents("job_ready") as unknown as JobEvent[];
const fold = (upTo: number): JobView => events.slice(0, upTo).reduce<JobView | null>((v, e) => foldJob(v, e), null) as JobView;

describe("foldJob over the recorded job_ready events", () => {
  it("is empty until keyword_ready", () => {
    expect(fold(6)).toEqual({ keywordReady: false, preflight: null, embedding: null, outcome: null });
  });

  it("keyword_ready gives the preflight and allows questions, with no embedding yet (waiting for its turn)", () => {
    const view = fold(7);
    expect(view.keywordReady).toBe(true);
    expect(view.embedding).toBeNull();
    expect(view.preflight).toMatchObject({ form: null, sections: [], chunks: 12, pendingChunks: 12 });
    expect(view.preflight?.sectionsMessage).toContain("not recognised");
  });

  it("embedding events carry done, total and the estimate", () => {
    expect(fold(9).embedding).toEqual({ done: 6, total: 12, etaS: 4.5 });
  });

  it("ready ends with the outcome and the search label", () => {
    expect(fold(events.length).outcome).toEqual({ embeddedChunks: 12, keywordOnlyChunks: 0, stopped: null, search: "keyword_semantic" });
  });

  it("a replayed earlier event never moves a finished view", () => {
    const done = fold(events.length);
    expect(foldJob(done, events[7])).toBe(done);
  });

  it("does not mutate the previous view", () => {
    const before = fold(7);
    const copy = structuredClone(before);
    foldJob(before, events[8]);
    expect(before).toEqual(copy);
  });

  it("a replayed earlier embedding event does not move progress back", () => {
    const behind = { ...events[8], progress: { done: 2, total: 12, eta_s: 9 } } as unknown as JobEvent;
    expect(foldJob(fold(9), behind).embedding).toEqual({ done: 6, total: 12, etaS: 4.5 });
  });
});

describe("announceStep: what a screen reader is told while search by meaning is prepared", () => {
  const base: JobView = { keywordReady: true, preflight: null, embedding: null, outcome: null };
  const at = (done: number, total = 100): JobView => ({ ...base, embedding: { done, total, etaS: null } });
  const MINUTE = 60_000;

  it("says nothing before the embedding starts", () => {
    expect(announceStep(ANNOUNCE_START, base, 0).note).toBe("");
  });

  it("announces the start once, then progress only at 25% steps and at most once a minute", () => {
    let s = announceStep(ANNOUNCE_START, at(0), 1_000);
    expect(s.note).toBe("Preparing search by meaning started.");
    s = announceStep(s, at(10), 2_000);
    expect(s.note).toBe("Preparing search by meaning started."); // not a 25% step: nothing new
    s = announceStep(s, at(30), 10_000);
    expect(s.note).toBe("Preparing search by meaning started."); // a step, but under a minute since the last line
    s = announceStep(s, at(31), 1_000 + MINUTE);
    expect(s.note).toBe("Preparing search by meaning: 31 of 100 passages");
    const after = announceStep(s, at(40), 1_000 + 3 * MINUTE);
    expect(after).toBe(s); // still the 25% step already spoken
    s = announceStep(after, at(80), 1_000 + 4 * MINUTE);
    expect(s.note).toBe("Preparing search by meaning: 80 of 100 passages");
  });

  it("a replay of earlier progress never repeats a spoken step", () => {
    let s = announceStep(ANNOUNCE_START, at(0), 0);
    s = announceStep(s, at(50), MINUTE);
    const spoken = s.note;
    expect(announceStep(s, at(26), 5 * MINUTE).note).toBe(spoken);
  });

  it("clears its note when the job has an outcome (the end line speaks then)", () => {
    const s = announceStep(ANNOUNCE_START, at(0), 0);
    const outcome = { embeddedChunks: 1, keywordOnlyChunks: 0, stopped: null, search: null };
    expect(announceStep(s, { ...base, outcome }, 1).note).toBe("");
  });
});
