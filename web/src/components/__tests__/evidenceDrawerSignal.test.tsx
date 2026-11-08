import { act, render, screen } from "@testing-library/react";
import { StrictMode, useEffect, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EvidenceDrawerProvider } from "@/components/EvidenceDrawer";
import { useEvidenceDrawer, type EvidenceDrawerApi } from "@/components/useEvidenceDrawer";
import { EvidenceCache } from "@/lib/evidenceCache";
import { useWorkspace, type WorkspaceContextValue } from "@/state/useWorkspace";
import { WorkspaceProvider } from "@/state/WorkspaceProvider";

// I3 round 2, blocking issue 1: the upload queue and the job watchers live in WorkspaceProvider, ABOVE the drawer, so they
// cannot call the drawer. They bump a signal on the workspace context instead and the drawer (below it, as in App.tsx) reads it.
// A job that ends while the visitor is on another route must still drop the cached `doc:` evidence.

const A = "a".repeat(32);
const B = "b".repeat(32);
const DOC = "doc:0123456789ab:v2:0007";

afterEach(() => vi.restoreAllMocks());

function capture<T>(use: () => T, into: { current: T | null }) {
  return function Capture() {
    const value = use();
    useEffect(() => {
      into.current = value;
    });
    return null;
  };
}

function mount(children: ReactNode) {
  return render(
    <StrictMode>
      <WorkspaceProvider>
        <EvidenceDrawerProvider>{children}</EvidenceDrawerProvider>
      </WorkspaceProvider>
    </StrictMode>,
  );
}

function mountBoth() {
  const ws: { current: WorkspaceContextValue | null } = { current: null };
  const drawer: { current: EvidenceDrawerApi | null } = { current: null };
  const Watcher = capture(useWorkspace, ws);
  const Opener = capture(useEvidenceDrawer, drawer);
  mount(
    <>
      <Watcher />
      <Opener />
    </>,
  );
  return { ws, drawer };
}

describe("the drawer follows the workspace's doc-evidence signal", () => {
  it("drops its doc: cache when a job reaches a terminal state, with no call into the drawer", () => {
    const dropDoc = vi.spyOn(EvidenceCache.prototype, "dropDoc");
    const { ws } = mountBoth();
    dropDoc.mockClear();
    act(() => ws.current?.invalidateDocEvidence());
    expect(dropDoc).toHaveBeenCalledTimes(1);
  });

  it("keeps the drawer open when only the doc evidence is invalidated", () => {
    const { ws, drawer } = mountBoth();
    act(() => ws.current?.setWorkspace({ id: A, token: "t", expires_at: null }));
    act(() => drawer.current?.open(DOC, null, "changes"));
    act(() => ws.current?.invalidateDocEvidence());
    expect(screen.getByRole("complementary", { name: "Evidence" })).toBeInTheDocument();
  });

  it("closes the drawer and clears everything when the workspace identity changes", () => {
    const clear = vi.spyOn(EvidenceCache.prototype, "clear");
    const { ws, drawer } = mountBoth();
    act(() => ws.current?.setWorkspace({ id: A, token: "t", expires_at: null }));
    act(() => drawer.current?.open(DOC, null, "changes"));
    expect(screen.getByRole("complementary", { name: "Evidence" })).toBeInTheDocument();
    clear.mockClear();
    act(() => ws.current?.setWorkspace({ id: B, token: "t2", expires_at: null }));
    expect(screen.queryByRole("complementary", { name: "Evidence" })).toBeNull();
    expect(clear).toHaveBeenCalled();
  });

  it("leaves an open drawer alone when the same workspace gets its expiry filled in", () => {
    const { ws, drawer } = mountBoth();
    act(() => ws.current?.setWorkspace({ id: A, token: "t", expires_at: null }));
    act(() => drawer.current?.open(DOC, null, "changes"));
    act(() => ws.current?.setWorkspace({ id: A, token: "t", expires_at: "2026-11-09T00:00:00Z" }));
    expect(screen.getByRole("complementary", { name: "Evidence" })).toBeInTheDocument();
  });
});
