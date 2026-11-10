import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode, useEffect, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AnswerText, Chip } from "@/components/Chips";
import { EvidenceDrawerProvider } from "@/components/EvidenceDrawer";
import { useEvidenceDrawer, type EvidenceDrawerApi } from "@/components/useEvidenceDrawer";
import { resetChipEvidence } from "@/lib/chipEvidence";
import { ANSWER_HEADING_ID } from "@/lib/focusTargets";
import { WorkspaceContext, type WorkspaceContextValue } from "@/state/useWorkspace";
import { CHUNK, DOC, FR, XBRL } from "./answerCorpus";

// R24, A5, A2, S4, S11: the evidence drawer. Three titles of tests/ui/evidence.test.mjs (the escaping of every field, and the two
// dropDocEvidence titles) are ported here, with the drawer's own behaviour: the landmark, focus, Escape, openSeq, the stale opener,
// the request guards and the workspace route.

const PAYLOAD = {
  type: "chunk",
  text: "We are subject to laws on privacy <b>fines</b>.",
  source_url: "https://www.sec.gov/Archives/x.htm",
  form: "10-K",
  filing_date: "2026-02-25",
  filer: "NVIDIA Corp",
  section_title: "Risk Factors",
  status: "current",
  is_current: true,
  item_headlines: [],
};
const WS_ID = "a".repeat(32);

function workspaceValue(over: Partial<WorkspaceContextValue> = {}): WorkspaceContextValue {
  return {
    workspace: null,
    asOf: "",
    setAsOf: () => {},
    setWorkspace: () => {},
    generation: 0,
    docEvidenceEpoch: 0,
    invalidateDocEvidence: () => {},
    headers: () => ({}),
    ...over,
  };
}
const openSession = (): Partial<WorkspaceContextValue> => ({
  workspace: { id: WS_ID, token: "secret-token", expires_at: null },
  headers: () => ({ "X-Workspace-Token": "secret-token" }),
});

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  resetChipEvidence();
  fetchMock = vi.fn(async () => json(PAYLOAD));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

function mount(children: ReactNode, value = workspaceValue()) {
  const tree = (v: WorkspaceContextValue) => (
    <StrictMode>
      <WorkspaceContext.Provider value={v}>
        <EvidenceDrawerProvider>{children}</EvidenceDrawerProvider>
      </WorkspaceContext.Provider>
    </StrictMode>
  );
  const view = render(tree(value));
  return { ...view, rerenderWith: (v: WorkspaceContextValue) => view.rerender(tree(v)) };
}

/** A component that hands the drawer API to the test (a factory: a component must not write to its props). */
function probeFor(into: { current: EvidenceDrawerApi | null }) {
  return function Probe() {
    const api = useEvidenceDrawer();
    useEffect(() => {
      into.current = api;
    });
    return null;
  };
}
const calls = () => fetchMock.mock.calls.map((c) => String(c[0]));
const drawer = () => document.querySelector("aside[aria-label='Evidence']") as HTMLElement;

describe("tests/ui/evidence.test.mjs", () => {
  it("every server-supplied field is escaped in the drawer markup", async () => {
    const evil = '<img src=x onerror=alert(1)>"><script>alert(2)</script>';
    fetchMock.mockImplementation(async () =>
      json({ ...PAYLOAD, filer: evil, section_title: evil, form: evil, mentions: [evil], status: evil, superseded_by: evil, corrected_by: evil, valid_to: evil, item_headlines: [evil], source_url: 'https://x.test/"><script>1</script>', text: evil }),
    );
    const user = userEvent.setup();
    mount(<Chip id={CHUNK} place="answer" gen={1} nth={0} />);
    await user.click(screen.getByRole("button", { name: /Item 1A/ }));
    await waitFor(() => expect(drawer().textContent).toContain(evil));
    expect(drawer().querySelector("script")).toBeNull();
    expect(drawer().querySelector("img")).toBeNull();
    for (const el of Array.from(drawer().querySelectorAll("*"))) {
      for (const attr of Array.from(el.attributes)) expect(/^on/i.test(attr.name) || attr.name === "src" || attr.name === "style").toBe(false);
    }
    for (const a of Array.from(drawer().querySelectorAll("a"))) {
      expect(a.getAttribute("href")).toMatch(/^https:\/\//);
      expect(a.getAttribute("rel")).toBe("noopener noreferrer");
    }
  });

  it("dropDocEvidence removes every cached doc: entry and keeps chunk/xbrl/fr entries", async () => {
    const api: { current: EvidenceDrawerApi | null } = { current: null };
    const Probe = probeFor(api);
    mount(<Probe />, workspaceValue(openSession()));
    for (const id of [DOC, CHUNK, XBRL, FR]) {
      act(() => api.current?.open(id, null, "answer"));
      await waitFor(() => expect(calls().some((u) => u.includes(encodeURIComponent(id)))).toBe(true));
    }
    const before = calls().length;
    act(() => api.current?.dropDocEvidence());
    for (const id of [CHUNK, XBRL, FR, DOC]) act(() => api.current?.open(id, null, "answer"));
    await waitFor(() => expect(calls().length).toBe(before + 1)); // only the doc: id is fetched again
    expect(calls().at(-1)).toContain(encodeURIComponent(DOC));
  });

  it("dropDocEvidence is a no-op on a cache with no doc: entries", async () => {
    const api: { current: EvidenceDrawerApi | null } = { current: null };
    const Probe = probeFor(api);
    mount(<Probe />);
    act(() => api.current?.open(CHUNK, null, "answer"));
    await waitFor(() => expect(calls()).toHaveLength(1));
    act(() => api.current?.dropDocEvidence());
    act(() => api.current?.open(CHUNK, null, "answer"));
    expect(calls()).toHaveLength(1);
  });
});

describe("A5: a non-modal complementary landmark", () => {
  it("is closed by default: inert, hidden from the accessibility tree and out of the tab order", () => {
    mount(<Chip id={CHUNK} place="answer" gen={1} nth={0} />);
    expect(drawer()).toHaveAttribute("inert");
    expect(drawer()).toHaveAttribute("aria-hidden", "true");
    expect(screen.queryByRole("complementary", { name: "Evidence" })).toBeNull();
  });

  it("opens on a click, names the landmark, moves focus to the close button and shows the evidence", async () => {
    const user = userEvent.setup();
    mount(<Chip id={CHUNK} place="answer" gen={1} nth={0} />);
    await user.click(screen.getByRole("button", { name: "Item 1A \u00b60361" }));
    expect(screen.getByRole("complementary", { name: "Evidence" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close evidence" })).toHaveFocus();
    expect(await screen.findByText(PAYLOAD.text)).toBeInTheDocument();
    expect(drawer().textContent).toContain("NVIDIA Corp");
  });

  it("Escape closes it and focus returns to the chip that opened it", async () => {
    const user = userEvent.setup();
    mount(<Chip id={CHUNK} place="answer" gen={1} nth={0} />);
    const chip = screen.getByRole("button", { name: /Item 1A/ });
    await user.click(chip);
    await user.keyboard("{Escape}");
    expect(drawer()).toHaveAttribute("inert");
    expect(chip).toHaveFocus();
  });

  it("an Escape something above it already handled (an open popover, SR9) leaves it open", async () => {
    const user = userEvent.setup();
    mount(<Chip id={CHUNK} place="answer" gen={1} nth={0} />);
    await user.click(screen.getByRole("button", { name: /Item 1A/ }));
    const handled = (event: KeyboardEvent) => event.preventDefault();
    document.addEventListener("keydown", handled, true); // the capture listener an open specimen popover installs
    await user.keyboard("{Escape}");
    document.removeEventListener("keydown", handled, true);
    expect(drawer()).not.toHaveAttribute("inert");
    await user.keyboard("{Escape}"); // the next Escape, with nothing above it, closes it
    expect(drawer()).toHaveAttribute("inert");
  });

  it("the close button closes it and focus returns to the chip", async () => {
    const user = userEvent.setup();
    mount(<Chip id={CHUNK} place="answer" gen={1} nth={0} />);
    const chip = screen.getByRole("button", { name: /Item 1A/ });
    await user.click(chip);
    await user.click(screen.getByRole("button", { name: "Close evidence" }));
    expect(drawer()).toHaveAttribute("inert");
    expect(chip).toHaveFocus();
  });

  it("does not take focus back when the visitor has already moved to another control", async () => {
    const user = userEvent.setup();
    mount(
      <>
        <Chip id={CHUNK} place="answer" gen={1} nth={0} />
        <textarea aria-label="q" />
      </>,
    );
    await user.click(screen.getByRole("button", { name: /Item 1A/ }));
    const box = screen.getByRole("textbox", { name: "q" });
    act(() => box.focus());
    await user.keyboard("{Escape}");
    expect(box).toHaveFocus();
  });

  it("the answer and every other chip stay operable while it is open (no focus trap, nothing inert)", async () => {
    const user = userEvent.setup();
    mount(<AnswerText text={`One [${CHUNK}] two [${XBRL}].`} place="answer" gen={1} />);
    await user.click(screen.getByRole("button", { name: /Item 1A/ }));
    expect(document.querySelectorAll("[inert]")).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: /XBRL revenue/ }));
    expect(await screen.findByText(`id: ${XBRL}`)).toBeInTheDocument(); // the second chip's evidence now fills the panel
    expect(calls().length).toBe(2);
  });

  it("a chip is operated from the keyboard: Enter and Space open it (A1)", async () => {
    const user = userEvent.setup();
    mount(<Chip id={CHUNK} place="answer" gen={1} nth={0} />);
    const chip = screen.getByRole("button", { name: /Item 1A/ });
    act(() => chip.focus());
    await user.keyboard("{Enter}");
    expect(drawer()).not.toHaveAttribute("inert");
    await user.keyboard("{Escape}");
    act(() => chip.focus());
    await user.keyboard(" ");
    expect(drawer()).not.toHaveAttribute("inert");
  });
});

describe("openSeq: only the last click fills the drawer", () => {
  it("an earlier fetch that returns later is never shown over the newer one", async () => {
    const resolvers: Record<string, (r: Response) => void> = {};
    fetchMock.mockImplementation((url: string) => new Promise<Response>((resolve) => void (resolvers[url] = resolve)));
    const api: { current: EvidenceDrawerApi | null } = { current: null };
    const Probe = probeFor(api);
    mount(<Probe />);
    act(() => api.current?.open(CHUNK, null, "answer"));
    act(() => api.current?.open(XBRL, null, "answer"));
    await act(async () => {
      resolvers[`/api/evidence/${encodeURIComponent(XBRL)}`](json({ company: "NEWER", value: 1, unit: "USD" }));
    });
    await act(async () => {
      resolvers[`/api/evidence/${encodeURIComponent(CHUNK)}`](json({ ...PAYLOAD, filer: "OLDER" }));
    });
    expect(drawer().textContent).toContain("NEWER");
    expect(drawer().textContent).not.toContain("OLDER");
    // the earlier request was abandoned (aborted), so its answer was never cached: opening it again asks again
    act(() => api.current?.open(CHUNK, null, "answer"));
    expect(calls()).toHaveLength(3);
    expect(drawer().textContent).not.toContain("OLDER");
  });

  it("a failed request says so with the server's detail", async () => {
    fetchMock.mockImplementation(async () => json({ detail: "no evidence with that id" }, 404));
    const user = userEvent.setup();
    mount(<Chip id={CHUNK} place="answer" gen={1} nth={0} />);
    await user.click(screen.getByRole("button", { name: /Item 1A/ }));
    expect(await screen.findByText("Could not load this source: no evidence with that id")).toBeInTheDocument();
  });

  it("while loading it says so", async () => {
    fetchMock.mockImplementation(() => new Promise<Response>(() => {}));
    const user = userEvent.setup();
    mount(<Chip id={CHUNK} place="answer" gen={1} nth={0} />);
    await user.click(screen.getByRole("button", { name: /Item 1A/ }));
    expect(screen.getByText("Loading the source\u2026")).toBeInTheDocument(); // RD17 wording (was the legacy "loading...")
  });
});

describe("S4: an id comes from untrusted model text", () => {
  it.each([
    ["a path traversal", "../../api/admin/state"],
    ["an encoded slash", "0001045810-26-000021%2fI.1A:0361"],
    ["a query string", `${CHUNK}?x=1`],
    ["a trailing newline", `${CHUNK}\n`],
    ["a different case", CHUNK.toLowerCase().replace("i.1a", "i.1a")],
    ["an empty id", ""],
  ])("%s makes no request", (_name, id) => {
    const api: { current: EvidenceDrawerApi | null } = { current: null };
    const Probe = probeFor(api);
    mount(<Probe />, workspaceValue(openSession()));
    act(() => api.current?.open(id, null, "answer"));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(drawer().textContent).toContain("Could not load this source: malformed evidence id");
  });

  it("a doc: id with no workspace open makes no request and says what the server would", () => {
    const api: { current: EvidenceDrawerApi | null } = { current: null };
    const Probe = probeFor(api);
    mount(<Probe />);
    act(() => api.current?.open(DOC, null, "changes"));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(drawer().textContent).toContain("Could not load this source: not a public evidence id");
  });

  it("a public id is fetched from the public route with the id encoded and no token", async () => {
    const api: { current: EvidenceDrawerApi | null } = { current: null };
    const Probe = probeFor(api);
    mount(<Probe />, workspaceValue(openSession()));
    act(() => api.current?.open(CHUNK, null, "answer"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toBe(`/api/evidence/${encodeURIComponent(CHUNK)}`);
    expect((fetchMock.mock.calls[0][1] as RequestInit).headers).toBeUndefined(); // a public id carries no token header
  });

  it("a doc: id with a workspace open goes to the workspace route, with the token only in the header", async () => {
    const api: { current: EvidenceDrawerApi | null } = { current: null };
    const Probe = probeFor(api);
    mount(<Probe />, workspaceValue(openSession()));
    act(() => api.current?.open(DOC, null, "changes"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/workspace/${WS_ID}/evidence/${encodeURIComponent(DOC)}`);
    expect(url).not.toContain("secret-token");
    expect(init.headers).toEqual({ "X-Workspace-Token": "secret-token" });
    expect(drawer().textContent).not.toContain("secret-token");
  });
});

describe("A2: focus returns to the right chip", () => {
  const Answer = ({ text, gen }: { text: string; gen: number }) => (
    <>
      <h2 id={ANSWER_HEADING_ID} tabIndex={-1}>
        Ask the graph
      </h2>
      <AnswerText text={text} place="answer" gen={gen} />
    </>
  );
  const TEXT = `Revenue [${XBRL}] and risk [${CHUNK}] and again [${CHUNK}].`;

  it("after the answer re-rendered with more text, the same chip (same key) gets the focus", async () => {
    const user = userEvent.setup();
    const view = mount(<Answer text={TEXT} gen={1} />);
    const second = document.querySelector(`[data-chip-key="answer:1:${CHUNK}:1"]`) as HTMLElement;
    await user.click(second);
    view.rerender(
      <StrictMode>
        <WorkspaceContext.Provider value={workspaceValue()}>
          <EvidenceDrawerProvider>
            <Answer text={`${TEXT} More text streamed in.`} gen={1} />
          </EvidenceDrawerProvider>
        </WorkspaceContext.Provider>
      </StrictMode>,
    );
    await user.keyboard("{Escape}");
    expect(document.querySelector(`[data-chip-key="answer:1:${CHUNK}:1"]`)).toHaveFocus();
  });

  it("when a new answer replaced the opener, focus goes to the answer heading, never to the body", async () => {
    const user = userEvent.setup();
    const view = mount(<Answer text={TEXT} gen={1} />);
    await user.click(screen.getAllByRole("button", { name: /Item 1A/ })[0]);
    view.rerender(
      <StrictMode>
        <WorkspaceContext.Provider value={workspaceValue()}>
          <EvidenceDrawerProvider>
            <Answer text={`A different answer [${CHUNK}] and [${XBRL}].`} gen={2} />
          </EvidenceDrawerProvider>
        </WorkspaceContext.Provider>
      </StrictMode>,
    );
    await user.keyboard("{Escape}");
    expect(screen.getByRole("heading", { name: "Ask the graph" })).toHaveFocus();
  });
});

describe("the drawer follows the workspace", () => {
  it("a change of the workspace identity closes it and forgets everything", async () => {
    const api: { current: EvidenceDrawerApi | null } = { current: null };
    const Probe = probeFor(api);
    const view = mount(<Probe />, workspaceValue({ ...openSession(), generation: 1 }));
    act(() => api.current?.open(CHUNK, null, "answer"));
    await waitFor(() => expect(drawer()).not.toHaveAttribute("inert"));
    view.rerenderWith(workspaceValue({ generation: 2 }));
    await waitFor(() => expect(drawer()).toHaveAttribute("inert"));
    act(() => api.current?.open(CHUNK, null, "answer"));
    await waitFor(() => expect(calls()).toHaveLength(2)); // the cache was cleared
  });

  it("a route change closes it without taking focus back (close with restoreFocus false)", async () => {
    const user = userEvent.setup();
    const api: { current: EvidenceDrawerApi | null } = { current: null };
    const Probe = probeFor(api);
    mount(
      <>
        <Chip id={CHUNK} place="answer" gen={1} nth={0} />
        <button type="button">elsewhere</button>
        <Probe />
      </>,
    );
    const chip = screen.getByRole("button", { name: /Item 1A/ });
    await user.click(chip);
    act(() => api.current?.close({ restoreFocus: false }));
    expect(drawer()).toHaveAttribute("inert");
    expect(chip).not.toHaveFocus();
  });
});

describe("a chunk chip learns its form and period once its evidence has loaded", () => {
  it("every chip of that id changes its label, and only chunk chips do", async () => {
    const user = userEvent.setup();
    mount(<AnswerText text={`A [${CHUNK}] and [${CHUNK}] and [${XBRL}].`} place="answer" gen={1} />);
    await user.click(screen.getAllByRole("button", { name: "Item 1A \u00b60361" })[0]);
    await waitFor(() => expect(screen.getAllByRole("button", { name: "10-K filed 2026-02-25 \u00b7 Item 1A \u00b60361" })).toHaveLength(2));
    expect(screen.getByRole("button", { name: "XBRL revenue FY ended 2026-01-25" })).toBeInTheDocument();
  });
});

describe("the evidence GET carries an AbortSignal", () => {
  /** A fetch that never answers on its own; `signals` are the signals it was given, in order. */
  function hangingFetch() {
    const signals: AbortSignal[] = [];
    fetchMock.mockImplementation(
      (_url: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          const signal = init?.signal as AbortSignal;
          signals.push(signal);
          signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
        }),
    );
    return signals;
  }

  it("closing the drawer aborts the request in flight", async () => {
    const signals = hangingFetch();
    const api: { current: EvidenceDrawerApi | null } = { current: null };
    const Probe = probeFor(api);
    mount(<Probe />);
    act(() => api.current?.open(CHUNK, null, "answer"));
    await waitFor(() => expect(signals).toHaveLength(1));
    expect(signals[0].aborted).toBe(false);
    act(() => api.current?.close());
    expect(signals[0].aborted).toBe(true);
  });

  it("opening another id aborts the earlier request", async () => {
    const signals = hangingFetch();
    const api: { current: EvidenceDrawerApi | null } = { current: null };
    const Probe = probeFor(api);
    mount(<Probe />);
    act(() => api.current?.open(CHUNK, null, "answer"));
    await waitFor(() => expect(signals).toHaveLength(1));
    act(() => api.current?.open(XBRL, null, "answer"));
    await waitFor(() => expect(signals).toHaveLength(2));
    expect(signals[0].aborted).toBe(true);
    expect(signals[1].aborted).toBe(false);
  });

  it("the provider unmounting aborts the request in flight", async () => {
    const signals = hangingFetch();
    const api: { current: EvidenceDrawerApi | null } = { current: null };
    const Probe = probeFor(api);
    const { unmount } = mount(<Probe />);
    act(() => api.current?.open(CHUNK, null, "answer"));
    await waitFor(() => expect(signals).toHaveLength(1));
    unmount();
    expect(signals[0].aborted).toBe(true);
  });
});
