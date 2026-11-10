import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode, useEffect, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AnswerText, Chip } from "@/components/Chips";
import { EvidenceDrawerProvider } from "@/components/EvidenceDrawer";
import { useEvidenceDrawer, type EvidenceDrawerApi } from "@/components/useEvidenceDrawer";
import { AskProvider } from "../AskProvider";
import { useAsk, type AskContextValue } from "../useAsk";
import { useWorkspace, type WorkspaceContextValue } from "../useWorkspace";
import { WorkspaceProvider } from "../WorkspaceProvider";

// The frozen contracts of I3 (see the JSDoc of each use* file): the shape every worker codes against. These tests pin the SHAPE
// and the stub-level behaviour the contract promises (WorkspaceProvider and the chip/drawer wiring work as documented;
// AskProvider is idle until worker B implements it). Workers keep these green and add their own test files.

const CHUNK = "0001045810-26-000021:I.1A:0361";
const DOC = "doc:0123456789ab:v2:0007";

// The real provider now persists the session (R32), so a test must not inherit the previous test's.
afterEach(() => {
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
});

function capture<T>(use: () => T, into: { current: T | null }) {
  return function Capture() {
    const value = use();
    useEffect(() => {
      into.current = value;
    });
    return null;
  };
}

function inAllProviders(children: ReactNode) {
  return (
    <StrictMode>
      <WorkspaceProvider>
        <AskProvider>
          <EvidenceDrawerProvider>{children}</EvidenceDrawerProvider>
        </AskProvider>
      </WorkspaceProvider>
    </StrictMode>
  );
}

describe("WorkspaceProvider contract", () => {
  const seen: { current: WorkspaceContextValue | null } = { current: null };
  const Probe = capture(useWorkspace, seen);
  const mount = () =>
    render(
      <WorkspaceProvider>
        <Probe />
      </WorkspaceProvider>,
    );

  it("starts with no workspace, no as-of date, generation 0 and no headers", () => {
    mount();
    expect(seen.current?.workspace).toBeNull();
    expect(seen.current?.asOf).toBe("");
    expect(seen.current?.generation).toBe(0);
    expect(seen.current?.headers()).toEqual({});
  });

  it("setWorkspace opens a workspace: the token goes only into the X-Workspace-Token header, and the generation moves", () => {
    mount();
    act(() => seen.current?.setWorkspace({ id: "a".repeat(32), token: "tok", expires_at: "2026-11-09T00:00:00Z" }));
    expect(seen.current?.workspace?.id).toBe("a".repeat(32));
    expect(seen.current?.headers()).toEqual({ "X-Workspace-Token": "tok" });
    expect(seen.current?.generation).toBe(1);
    act(() => seen.current?.setWorkspace(null));
    expect(seen.current?.workspace).toBeNull();
    expect(seen.current?.headers()).toEqual({});
    expect(seen.current?.generation).toBe(2);
  });

  it("setAsOf keeps the date text", () => {
    mount();
    act(() => seen.current?.setAsOf("2026-06-30"));
    expect(seen.current?.asOf).toBe("2026-06-30");
  });

  it("outside a provider the hook is inert: no workspace, setters do nothing", () => {
    const inert: { current: WorkspaceContextValue | null } = { current: null };
    const Outside = capture(useWorkspace, inert);
    render(<Outside />);
    expect(inert.current?.workspace).toBeNull();
    expect(() => inert.current?.setWorkspace(null)).not.toThrow();
    expect(() => inert.current?.setAsOf("2026-01-01")).not.toThrow();
    expect(inert.current?.headers()).toEqual({});
  });
});

describe("AskProvider contract", () => {
  it("is idle before any ask, with the documented empty state", () => {
    const seen: { current: AskContextValue | null } = { current: null };
    const Probe = capture(useAsk, seen);
    render(
      <AskProvider>
        <Probe />
      </AskProvider>,
    );
    expect(seen.current).toMatchObject({
      status: "idle",
      seq: 0,
      question: "",
      strategy: "",
      text: "",
      done: null,
      retrieval: null,
      escalated: null,
      steps: [],
      refusal: null,
      statusText: null,
    });
    expect(typeof seen.current?.ask).toBe("function");
    expect(typeof seen.current?.abort).toBe("function");
    expect(typeof seen.current?.reset).toBe("function");
  });

  it("outside a provider the hook answers idle and its functions do nothing", () => {
    const seen: { current: AskContextValue | null } = { current: null };
    const Outside = capture(useAsk, seen);
    render(<Outside />);
    expect(seen.current?.status).toBe("idle");
    expect(() => seen.current?.abort()).not.toThrow();
  });

  it("sends no request just by mounting, under StrictMode (S12)", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(inAllProviders(<p>page</p>));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("EvidenceDrawer and Chips contract", () => {
  it("renders the named Evidence landmark, closed: inert and hidden from the accessibility tree", () => {
    render(inAllProviders(<p>page</p>));
    const aside = document.querySelector("aside[aria-label='Evidence']");
    expect(aside).not.toBeNull();
    expect(aside?.hasAttribute("inert")).toBe(true);
    expect(aside?.getAttribute("aria-hidden")).toBe("true");
    expect(screen.queryByRole("complementary", { name: "Evidence" })).toBeNull();
  });

  it("a chip is a real button with a label; clicking it opens the drawer; close, open and clearAll work", async () => {
    const seen: { current: EvidenceDrawerApi | null } = { current: null };
    const Probe = capture(useEvidenceDrawer, seen);
    render(
      inAllProviders(
        <>
          <Probe />
          <Chip id={CHUNK} place="answer" gen={1} nth={0} />
        </>,
      ),
    );
    const chip = screen.getByRole("button", { name: /paragraph 361/ });
    expect(chip.tagName).toBe("BUTTON");
    expect(chip.getAttribute("type")).toBe("button");
    await userEvent.click(chip);
    expect(screen.getByRole("complementary", { name: "Evidence" })).toBeInTheDocument();
    act(() => seen.current?.close());
    expect(screen.queryByRole("complementary", { name: "Evidence" })).toBeNull();
    act(() => seen.current?.open(DOC, chip, "changes"));
    expect(screen.getByRole("complementary", { name: "Evidence" })).toBeInTheDocument();
    act(() => seen.current?.clearAll());
    expect(screen.queryByRole("complementary", { name: "Evidence" })).toBeNull();
    expect(() => seen.current?.dropDocEvidence()).not.toThrow();
  });

  it("a chip key is the place, the answer sequence, the id and the occurrence (A2)", () => {
    render(inAllProviders(<Chip id={CHUNK} place="answer" gen={3} nth={1} />));
    expect(document.querySelector(`[data-chip-key="answer:3:${CHUNK}:1"]`)).not.toBeNull();
  });

  it("AnswerText turns each [id] token into a chip and leaves the rest as text nodes", () => {
    render(inAllProviders(<AnswerText text={`Revenue rose [${CHUNK}] and fell [${CHUNK}] <img src=x onerror=alert(1)>`} place="answer" gen={1} />));
    expect(screen.getAllByRole("button")).toHaveLength(2);
    expect(document.querySelector("img")).toBeNull();
    expect(document.querySelector(`[data-chip-key="answer:1:${CHUNK}:0"]`)).not.toBeNull();
    expect(document.querySelector(`[data-chip-key="answer:1:${CHUNK}:1"]`)).not.toBeNull();
    expect(screen.getByText(/<img src=x onerror=alert\(1\)>/)).toBeInTheDocument();
  });

  it("the drawer hook outside a provider is inert", () => {
    const seen: { current: EvidenceDrawerApi | null } = { current: null };
    const Outside = capture(useEvidenceDrawer, seen);
    render(<Outside />);
    expect(() => seen.current?.open(CHUNK, null, "answer")).not.toThrow();
  });
});

// Round 2 (blocking issue 2): the one reading of the workspace contract. The identity is the workspace id: the generation, the
// doc-evidence epoch and the as-of value all move on an identity change and on nothing else.
describe("WorkspaceProvider identity rules", () => {
  const seen: { current: WorkspaceContextValue | null } = { current: null };
  const Probe = capture(useWorkspace, seen);
  const A = "a".repeat(32);
  const B = "b".repeat(32);
  const mount = () =>
    render(
      <StrictMode>
        <WorkspaceProvider>
          <Probe />
        </WorkspaceProvider>
      </StrictMode>,
    );

  it("a restore followed by the same workspace with its expiry filled in is ONE identity change", () => {
    mount();
    act(() => seen.current?.setWorkspace({ id: A, token: "tok", expires_at: null }));
    act(() => seen.current?.setWorkspace({ id: A, token: "tok", expires_at: "2026-11-09T00:00:00Z" }));
    expect(seen.current?.generation).toBe(1);
    expect(seen.current?.docEvidenceEpoch).toBe(1);
    expect(seen.current?.workspace?.expires_at).toBe("2026-11-09T00:00:00Z");
  });

  it("a different id, a delete and a null-to-null call: +1, +1, +0", () => {
    mount();
    act(() => seen.current?.setWorkspace({ id: A, token: "t", expires_at: null }));
    act(() => seen.current?.setWorkspace({ id: B, token: "t", expires_at: null }));
    expect(seen.current?.generation).toBe(2);
    act(() => seen.current?.setWorkspace(null));
    expect(seen.current?.generation).toBe(3);
    act(() => seen.current?.setWorkspace(null));
    expect(seen.current?.generation).toBe(3);
  });

  it("asOf is sent verbatim (a date or an instant) and is reset by an identity change only", () => {
    mount();
    act(() => seen.current?.setWorkspace({ id: A, token: "t", expires_at: null }));
    act(() => seen.current?.setAsOf("2026-06-30T12:00:00Z"));
    expect(seen.current?.asOf).toBe("2026-06-30T12:00:00Z");
    act(() => seen.current?.setWorkspace({ id: A, token: "t", expires_at: "2026-11-09T00:00:00Z" }));
    expect(seen.current?.asOf).toBe("2026-06-30T12:00:00Z");
    act(() => seen.current?.setWorkspace({ id: B, token: "t", expires_at: null }));
    expect(seen.current?.asOf).toBe("");
    act(() => seen.current?.setAsOf("2026-06-30"));
    act(() => seen.current?.setWorkspace(null));
    expect(seen.current?.asOf).toBe("");
  });

  it("invalidateDocEvidence bumps the epoch and nothing else", () => {
    mount();
    act(() => seen.current?.setWorkspace({ id: A, token: "t", expires_at: null }));
    act(() => seen.current?.setAsOf("2026-06-30"));
    act(() => seen.current?.invalidateDocEvidence());
    act(() => seen.current?.invalidateDocEvidence());
    expect(seen.current?.docEvidenceEpoch).toBe(3);
    expect(seen.current?.generation).toBe(1);
    expect(seen.current?.asOf).toBe("2026-06-30");
    expect(seen.current?.workspace?.id).toBe(A);
  });

  it("outside a provider the signal is inert", () => {
    const inert: { current: WorkspaceContextValue | null } = { current: null };
    const Outside = capture(useWorkspace, inert);
    render(<Outside />);
    expect(inert.current?.docEvidenceEpoch).toBe(0);
    expect(() => inert.current?.invalidateDocEvidence()).not.toThrow();
  });
});
