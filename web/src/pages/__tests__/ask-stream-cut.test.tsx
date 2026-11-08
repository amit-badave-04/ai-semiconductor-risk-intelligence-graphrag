import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as answerBlocks from "@/lib/answerBlocks";
import * as answerMeta from "@/lib/answerMeta";
import { frame, scripted, type Step, CUT, END } from "@/lib/__tests__/sse-helpers";
import * as askState from "@/state/askState";
import { mountAsk } from "./askHarness";

// tests/ui/stream_cut.test.mjs, the DOM half (smoke check 6 of docs/specs/m5b-frontend.md R2): 14 titles, 16 tests (two titles are
// run for both endings). The read-loop half is web/src/lib/__tests__/sse.stream-cut.test.ts. Each title is the legacy title,
// character for character. The legacy page injected a failure by replacing page functions in its vm context; here a failure of the
// state (applyEvent) is the handler throwing, and a failure of the renderer or of the checks footer is a throw while React renders
// (a spy on parseAnswer or metaSummary), which the page's boundaries turn into the same honest status.
//
// The legacy `p.el("ask").disabled` is the Ask button; `status.innerHTML` with `<span class="err">` is the status region with the
// error colour; `answer.innerHTML` is the answer text; `meta.hidden` is the checks footer being absent.

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const RETRIEVAL = frame("retrieval", { anchors: { Nvidia: 1045810 }, counts: { chunks: 8 } });
const DELTA = frame("delta", { text: "Nvidia reported " });
const DONE = frame("done", { answer: "Nvidia reported growth.", cached: true, citations: [], hallucinated: [] });
const SERVER_ERROR = frame("error", { detail: "The service is paused for today." });
const PAGE_FAILED = /the page could not show this answer completely/;

/** An /api/ask that streams `steps` in order (bytes, CUT or END). */
function askWith(steps: Step[], onRead?: () => void) {
  const reader = scripted(steps);
  const read = reader.read.bind(reader);
  reader.read = async () => {
    onRead?.();
    return read();
  };
  return () => ({ ok: true, status: 200, statusText: "OK", body: { getReader: () => reader } }) as unknown as Response;
}

async function ask(steps: Step[], onRead?: () => void) {
  const env = mountAsk({ ask: askWith(steps, onRead) });
  await env.user.type(screen.getByRole("textbox", { name: "Your question" }), "What changed in Nvidia's risk factors?");
  await env.user.click(screen.getByRole("button", { name: "Ask" }));
  await waitFor(() => expect(env.posts()).toHaveLength(1));
  return env;
}
const askButton = () => screen.getByRole("button", { name: "Ask" });
const status = () => screen.getByRole("status");
const answer = () => document.querySelector("#answer") as HTMLElement;
const settled = () => waitFor(() => expect(askButton()).toBeEnabled());
const meta = () => document.querySelector("#meta");

describe("tests/ui/stream_cut.test.mjs", () => {
  it("a read that rejects mid-answer says the connection was lost and enables Ask again", async () => {
    let askDisabledWhileStreaming: boolean | null = null;
    await ask([RETRIEVAL, DELTA, CUT], () => {
      askDisabledWhileStreaming ??= (document.querySelector("button[type='submit'], #ask") as HTMLButtonElement | null)?.disabled ?? null;
    });
    await settled();
    expect(askDisabledWhileStreaming, "Ask is disabled while the answer streams (else this test proves nothing)").toBe(true);
    expect(status().textContent).toMatch(/^connection lost/);
    expect(status().className).toMatch(/destructive/);
    expect(status().textContent).not.toMatch(/generating/);
    expect(answer().textContent).toMatch(/Nvidia reported/); // the text that did arrive stays on the page
    expect(meta(), "an unfinished answer carries no checks footer").toBeNull();
  });

  it("the status never claims the cut answer is complete or checked", async () => {
    await ask([DELTA, CUT]);
    await settled();
    expect(status().textContent).toMatch(/incomplete/);
    expect(status().textContent).toMatch(/not checked/);
  });

  it("a stream that ends cleanly without a done or error event is reported the same way", async () => {
    await ask([RETRIEVAL, DELTA, END]);
    await settled();
    expect(askButton()).toBeEnabled();
    expect(status().textContent).toMatch(/^connection lost/);
    expect(status().className).toMatch(/destructive/);
    expect(status().textContent).not.toMatch(/generating/);
  });

  it("a cut before the first byte is reported too", async () => {
    await ask([CUT]);
    await settled();
    expect(status().textContent).toMatch(/^connection lost/);
    expect(status().className).toMatch(/destructive/);
  });

  for (const [ending, name] of [[CUT, "a reset"], [END, "closing"]] as const) {
    it(`a finished answer keeps its own status when the stream then ends by ${name}`, async () => {
      await ask([DELTA, DONE, ending]);
      await settled();
      expect(status().textContent).toBe("served from cache \u2014 no model call");
      expect(meta()).not.toBeNull();
      expect(answer().textContent).toMatch(/Nvidia reported growth\./);
    });

    it(`the server's own error detail is kept when the stream then ends by ${name}`, async () => {
      await ask([RETRIEVAL, SERVER_ERROR, ending]);
      await settled();
      expect(status().textContent).toBe("The service is paused for today.");
      expect(status().className).toMatch(/destructive/);
      expect(status().textContent).not.toMatch(/connection lost/);
    });
  }

  it("an ordinary answer is unchanged: done status, checks footer, Ask enabled", async () => {
    await ask([RETRIEVAL, DELTA, frame("done", { answer: "Nvidia reported growth.", cached: false, citations: [], hallucinated: [] }), END]);
    await settled();
    expect(status().textContent).toBe("done");
    expect(meta()).not.toBeNull();
    expect(askButton()).toBeEnabled();
  });

  it("a failure while handling an event still enables Ask again", async () => {
    vi.stubGlobal("reportError", vi.fn());
    vi.spyOn(askState, "applyEvent").mockImplementation(() => {
      throw new Error("boom"); // the state function the event handler calls
    });
    await ask([DELTA, DONE, END]);
    await settled();
    expect(askButton()).toBeEnabled();
  });
});

// The page itself can throw while it takes an answer in (a rendering bug, a malformed event). The status used to stay on
// "retrieving..." / "... generating..." for good, which says the answer is still on its way. Every way out must leave a status that
// is true: here, that the page could not show the answer completely.
function assertHonestAfterAPageFailure(why: string) {
  expect(askButton(), `${why}: Ask is enabled again`).toBeEnabled();
  expect(status().textContent, `${why}: the status says the page failed`).toMatch(PAGE_FAILED);
  expect(status().className).toMatch(/destructive/);
  expect(status().textContent, `${why}: never a status that is not true`).not.toMatch(/generating|retrieving|^done$/);
  expect(status().textContent, `${why}: it does not claim the checks ran`).toMatch(/not checked/);
}

describe("tests/ui/stream_cut.test.mjs: the page failing while it takes an answer in", () => {
  it("a failure while rendering a delta leaves an honest status, not generating\u2026", async () => {
    vi.spyOn(answerBlocks, "parseAnswer").mockImplementation(() => {
      throw new Error("boom");
    });
    await ask([RETRIEVAL, DELTA, DONE, END]);
    await settled();
    assertHonestAfterAPageFailure("delta");
  });

  it("a failure while rendering the finished answer does not leave the status on generating\u2026", async () => {
    const real = answerBlocks.parseAnswer;
    vi.spyOn(answerBlocks, "parseAnswer").mockImplementation((text) => {
      if (text === "Nvidia reported growth.") throw new Error("boom");
      return real(text);
    });
    await ask([RETRIEVAL, DELTA, DONE, END]);
    await settled();
    assertHonestAfterAPageFailure("done");
    expect(answer().textContent, "the text that did arrive stays on the page").toMatch(/Nvidia reported/);
  });

  it("a failure after the done status was set does not leave 'done' over an answer whose checks were never shown", async () => {
    vi.spyOn(answerMeta, "metaSummary").mockImplementation(() => {
      throw new Error("boom"); // the checks footer is built after the done status is set
    });
    await ask([RETRIEVAL, DELTA, DONE, END]);
    await settled();
    assertHonestAfterAPageFailure("finish");
    expect(meta(), "no footer was built").toBeNull();
  });

  it("a response without a body is reported and enables Ask again", async () => {
    const env = mountAsk({ ask: () => ({ ok: true, status: 200, statusText: "OK", body: null }) as unknown as Response });
    await env.user.type(screen.getByRole("textbox", { name: "Your question" }), "What changed in Nvidia's risk factors?");
    await env.user.click(askButton());
    await settled();
    expect(status().textContent).toMatch(PAGE_FAILED);
    expect(status().textContent).not.toMatch(/retrieving/);
  });

  it("the page failure is not hidden: ask() still rejects with the original error", async () => {
    const report = vi.fn();
    vi.stubGlobal("reportError", report);
    vi.spyOn(askState, "applyEvent").mockImplementation(() => {
      throw new Error("boom");
    });
    await ask([DELTA, END]);
    await settled();
    expect(report).toHaveBeenCalledTimes(1);
    expect((report.mock.calls[0][0] as Error).message).toBe("boom");
  });

  it("a server error event that was shown is not replaced by the page-failure text", async () => {
    await ask([RETRIEVAL, SERVER_ERROR, END]);
    await settled();
    expect(status().textContent).toBe("The service is paused for today.");
    expect(status().className).toMatch(/destructive/);
    expect(status().textContent).not.toMatch(/could not show/);
  });
});
