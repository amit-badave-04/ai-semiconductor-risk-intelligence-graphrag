import { describe, expect, it } from "vitest";
import { JOB_STATES } from "../events";
import { MAX_BUFFER_SIZE, PAGE_FAILED_STATUS, STREAM_CUT_STATUS } from "../sse";
import { MAX_ATTEMPTS } from "../jobStream";
import { ASK_FIXTURES, contractDoc, fixtureBytes, fixtureEvents, fixtureRefusals, JOB_FIXTURES } from "./fixtures";

// R18 (docs/specs/m5b-frontend.md): docs/v2/M5A_SSE_CONTRACT.md is written from the fixtures, and what it states about the client
// is the code. These tests fail when the document and the code (or the recorded bytes) drift apart.
const doc = contractDoc();
const wire = (text: string) => text.replaceAll("\u2014", "\\u2014"); // the JSON on the wire escapes the em dash

describe("docs/v2/M5A_SSE_CONTRACT.md matches the code and the fixtures", () => {
  it("states MAX_BUFFER_SIZE as the same number the code uses, and how it was sized", () => {
    expect(doc).toContain(String(MAX_BUFFER_SIZE));
    expect(doc).toContain("256 KiB");
    const largest = fixtureBytes("done_max_size").length;
    expect(doc).toContain(`${largest} bytes`);
    expect(MAX_BUFFER_SIZE / largest).toBeGreaterThan(10);
  });

  it("quotes every refusal detail verbatim, with its status and the draining Retry-After", () => {
    for (const refusal of fixtureRefusals()) {
      expect(doc, refusal.name).toContain(wire(refusal.detail));
      expect(doc, refusal.name).toContain(String(refusal.status));
    }
    expect(doc).toContain("Retry-After: 30");
  });

  it("quotes the two client statuses verbatim", () => {
    expect(doc).toContain(STREAM_CUT_STATUS);
    expect(doc).toContain(PAGE_FAILED_STATUS);
  });

  it("names every event kind the fixtures contain and every job state", () => {
    const kinds = new Set(ASK_FIXTURES.flatMap((name) => fixtureEvents(name).map((e) => String(e.event))));
    for (const kind of kinds) {
      expect(doc, kind).toContain(`\`${kind}\``);
    }
    for (const state of JOB_STATES) {
      expect(doc, state).toContain(`\`${state}\``);
    }
    for (const name of [...ASK_FIXTURES, ...JOB_FIXTURES]) {
      expect(doc, name).toContain(`${name}.sse`);
    }
  });

  it("lists the optional routing fields the fixtures carry and the refusals it does not record", () => {
    const keys = new Set(ASK_FIXTURES.flatMap((name) => fixtureEvents(name).flatMap((e) => Object.keys(e))));
    for (const key of ["answered_by", "routed", "escalated", "escalation_reasons", "anchor_defaulted"]) {
      expect(keys.has(key), `${key} is in the recorded bytes`).toBe(true);
      expect(doc, key).toContain(`\`${key}\``);
    }
    expect(doc).toContain("RECORDED subset");
    for (const name of ["MSG_BUDGET", "MSG_IP_BUDGET", "MSG_IP_SPEND", "MSG_READ_RATE", "MSG_STATE_UNAVAILABLE", "MSG_UPLOADS_OFF"]) {
      expect(doc, name).toContain(name);
    }
    expect(doc, "the input-check 400s and FastAPI's 422").toMatch(/guard\.py[\s\S]*422/);
  });

  it("pins the headers of the /api/ask response and the wire grammar", () => {
    for (const text of ["text/event-stream; charset=utf-8", "no-store", "X-Accel-Buffering", 'sep="\\n"']) {
      expect(doc).toContain(text);
    }
  });

  it("states the job-stream client limits the code has", () => {
    expect(doc).toContain(`${MAX_ATTEMPTS} attempts`);
    expect(doc).toContain("X-Workspace-Token");
  });

  it("carries the Frozen paragraph", () => {
    expect(doc).toMatch(/## Frozen/);
    expect(doc).toContain("spec amendment");
    expect(doc).toContain("owner");
  });
});
