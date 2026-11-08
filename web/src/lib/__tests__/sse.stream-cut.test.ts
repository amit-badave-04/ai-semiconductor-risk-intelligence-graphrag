import { describe, expect, it } from "vitest";
import { askStream, PAGE_FAILED_STATUS, STREAM_CUT_STATUS } from "../sse";
import type { StreamEvent } from "../events";
import { CUT, END, fetchOf, frame, scripted, type Step } from "./sse-helpers";

// The read-loop half of tests/ui/stream_cut.test.mjs (M5a closing review, V3), ported test for test onto the shipped sse.ts
// (R17, smoke check 6). The other half of each old test drives the legacy page's DOM: `#status` innerHTML, the Ask button's
// disabled state, the checks footer. That half comes with the Ask page in I3 and is marked "DOM half (I3)" per test below.
// Here the "page" is a small model of what the Ask page does with the events: it keeps the text, sets the status the server
// event implies, and shows the footer; the outcome of askStream then decides whether that status stands.
const RETRIEVAL = frame("retrieval", { anchors: { Nvidia: 1045810 }, counts: { chunks: 8 } });
const DELTA = frame("delta", { text: "Nvidia reported " });
const DONE = frame("done", { answer: "Nvidia reported growth.", citations: [], hallucinated: [], cached: true });
const DONE_LIVE = frame("done", { answer: "Nvidia reported growth.", citations: [], hallucinated: [], cached: false });
const SERVER_ERROR = frame("error", { detail: "The service is paused for today." });

interface Hooks {
  render?: (text: string) => void; // a page-level function the event handler calls (it may throw, as a rendering bug would)
  footer?: () => void;
  noBody?: boolean;
}

async function ask(steps: Step[], hooks: Hooks = {}) {
  const reader = scripted(steps);
  const { fetchImpl, calls } = fetchOf(hooks.noBody ? null : reader);
  const page = { status: "retrieving from the graph...", text: "", footer: false };
  const onEvent = (event: StreamEvent) => {
    if (event.event === "retrieval") {
      page.status = "retrieved - generating...";
    } else if (event.event === "delta") {
      page.text += event.text;
      hooks.render?.(page.text);
    } else if (event.event === "done") {
      page.text = event.answer;
      hooks.render?.(page.text);
      page.status = event.cached === true ? "served from cache - no model call" : "done";
      hooks.footer?.();
      page.footer = true;
    } else if (event.event === "error") {
      page.status = event.detail;
    }
  };
  const outcome = await askStream({ question: "What changed in Nvidia's risk factors?", strategy: "hybrid" }, { onEvent, fetchImpl });
  // the status the page would show: the outcome's own when the stream did not end itself, else the one the server event set
  const status = outcome.status ?? page.status;
  return { outcome, page, status, reader, calls };
}

describe("a cut answer stream (tests/ui/stream_cut.test.mjs, read-loop half)", () => {
  it("a read that rejects mid-answer says the connection was lost and keeps the text that arrived", async () => {
    // DOM half (I3): Ask disabled while streaming then enabled; status innerHTML is `<span class="err">connection lost...`
    const { status, page, outcome } = await ask([RETRIEVAL, DELTA, CUT]);
    expect(status).toBe(STREAM_CUT_STATUS);
    expect(status).not.toMatch(/generating/);
    expect(outcome).toMatchObject({ ended: false, failed: false });
    expect(page.text).toBe("Nvidia reported ");
    expect(page.footer).toBe(false); // an unfinished answer carries no checks footer
  });

  it("the status never claims the cut answer is complete or checked", async () => {
    const { status } = await ask([DELTA, CUT]);
    expect(status).toMatch(/incomplete/);
    expect(status).toMatch(/not checked/);
  });

  it("a stream that ends cleanly without a done or error event is reported the same way", async () => {
    // DOM half (I3): Ask enabled again
    const { status } = await ask([RETRIEVAL, DELTA, END]);
    expect(status).toBe(STREAM_CUT_STATUS);
    expect(status).not.toMatch(/generating/);
  });

  it("a cut before the first byte is reported too", async () => {
    expect((await ask([CUT])).status).toBe(STREAM_CUT_STATUS);
  });

  it.each([
    ["a reset", CUT],
    ["closing", END],
  ] as const)("a finished answer keeps its own status when the stream then ends by %s", async (_name, ending) => {
    // DOM half (I3): the status innerHTML and the footer's `hidden` flag
    const { status, page, outcome } = await ask([DELTA, DONE, ending]);
    expect(status).toBe("served from cache - no model call");
    expect(page.footer).toBe(true);
    expect(page.text).toBe("Nvidia reported growth.");
    expect(outcome.ended).toBe(true);
  });

  it.each([
    ["a reset", CUT],
    ["closing", END],
  ] as const)("the server's own error detail is kept when the stream then ends by %s", async (_name, ending) => {
    const { status, outcome } = await ask([RETRIEVAL, SERVER_ERROR, ending]);
    expect(status).toBe("The service is paused for today.");
    expect(status).not.toMatch(/connection lost/);
    expect(outcome.ended).toBe(true);
  });

  it("an ordinary answer is unchanged: done status and the checks footer", async () => {
    // DOM half (I3): Ask enabled
    const { status, page } = await ask([RETRIEVAL, DELTA, DONE_LIVE, END]);
    expect(status).toBe("done");
    expect(page.footer).toBe(true);
  });

  it("a failure while handling an event still lets the call settle (Ask can be enabled again)", async () => {
    // DOM half (I3): the Ask button is enabled in the page's finally
    const { outcome } = await ask([DELTA, DONE, END], {
      render: () => {
        throw new Error("boom");
      },
    });
    expect(outcome.failed).toBe(true);
  });
});

// The page itself can throw while it takes an answer in (a rendering bug, a malformed event). Every way out of the read must
// leave a status that is true: here, that the page could not show the answer completely.
function expectHonestAfterAPageFailure(result: Awaited<ReturnType<typeof ask>>, why: string) {
  expect(result.status, `${why}: the status says the page failed`).toBe(PAGE_FAILED_STATUS);
  expect(result.status, `${why}: never a status that is not true`).not.toMatch(/generating|retrieving|^done$/);
  expect(result.status, `${why}: it does not claim the checks ran`).toMatch(/not checked/);
  expect(result.outcome.failed).toBe(true);
}

describe("the page failing while it takes an answer in", () => {
  it("a failure while rendering a delta leaves an honest status, not generating", async () => {
    const result = await ask([RETRIEVAL, DELTA, DONE, END], {
      render: () => {
        throw new Error("boom");
      },
    });
    expectHonestAfterAPageFailure(result, "delta");
  });

  it("a failure while rendering the finished answer does not leave the status on generating", async () => {
    const result = await ask([RETRIEVAL, DELTA, DONE, END], {
      render: (text) => {
        if (text === "Nvidia reported growth.") {
          throw new Error("boom");
        }
      },
    });
    expectHonestAfterAPageFailure(result, "done");
    expect(result.page.text).toMatch(/Nvidia reported/); // the text that did arrive stays on the page
  });

  it("a failure after the done status was set does not leave 'done' over an answer whose checks were never shown", async () => {
    const result = await ask([RETRIEVAL, DELTA, DONE, END], {
      footer: () => {
        throw new Error("boom");
      },
    });
    expectHonestAfterAPageFailure(result, "finish");
    expect(result.page.footer).toBe(false); // no footer was built
    expect(result.outcome.ended).toBe(false);
  });

  it("a response without a body is reported and enables Ask again", async () => {
    // DOM half (I3): Ask enabled
    const result = await ask([], { noBody: true });
    expectHonestAfterAPageFailure(result, "no body");
    expect(result.status).not.toMatch(/retrieving/);
  });

  it("the page failure is not hidden: the outcome carries the original error", async () => {
    const boom = new Error("boom");
    const result = await ask([DELTA, END], {
      render: () => {
        throw boom;
      },
    });
    expect(result.outcome.thrown).toBe(boom);
  });

  it("a server error event that was shown is not replaced by the page-failure text", async () => {
    const { status } = await ask([RETRIEVAL, SERVER_ERROR, END]);
    expect(status).toBe("The service is paused for today.");
    expect(status).not.toMatch(/could not show/);
  });
});
