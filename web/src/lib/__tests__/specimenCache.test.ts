import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetSpecimenCache, specimenEvidence } from "../specimenCache";
import { SPECIMENS } from "../specimens";

// docs/specs/m5b-redesign.md RD4, SR3: a specimen's evidence is fetched once per page load; a failure is not kept.

let status = 200;
const fetchMock = vi.fn(async () => new Response(JSON.stringify({ type: "xbrl" }), { status }));

beforeEach(() => {
  resetSpecimenCache();
  status = 200;
  fetchMock.mockClear();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("specimenEvidence", () => {
  it("joins a request already in flight and serves a success from the cache", async () => {
    const [a, b] = await Promise.all([specimenEvidence(SPECIMENS.number), specimenEvidence(SPECIMENS.number)]);
    await specimenEvidence(SPECIMENS.number);
    expect(a.ok && b.ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not keep a failure, so the next opening asks again", async () => {
    status = 503;
    expect((await specimenEvidence(SPECIMENS.number)).ok).toBe(false);
    status = 200;
    expect((await specimenEvidence(SPECIMENS.number)).ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("asks only by the encoded public id (SR3)", async () => {
    await specimenEvidence(SPECIMENS.passage);
    expect(fetchMock).toHaveBeenCalledWith(`/api/evidence/${encodeURIComponent(SPECIMENS.passage)}`, {});
  });
});
