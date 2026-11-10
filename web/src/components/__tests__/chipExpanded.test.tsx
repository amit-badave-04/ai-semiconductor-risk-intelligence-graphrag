import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode, useEffect, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AnswerText, Chip } from "@/components/Chips";
import { EvidenceDrawerProvider } from "@/components/EvidenceDrawer";
import { useEvidenceDrawer, type EvidenceDrawerApi } from "@/components/useEvidenceDrawer";
import { resetChipEvidence } from "@/lib/chipEvidence";
import { WorkspaceContext, type WorkspaceContextValue } from "@/state/useWorkspace";
import { CHUNK, XBRL } from "./answerCorpus";

// docs/specs/m5b-redesign.md RD11: `aria-expanded` on the chip whose evidence is open (that occurrence only, by its chip key),
// pointing at the panel with aria-controls; A1 and A5 unchanged. The drawer API gains `openKey` and an optional fourth argument
// of `open`, the quoted words for RD18 (an additive change to the frozen contract).

const PAYLOAD = { type: "chunk", text: "We are subject to laws.", form: "10-K", filing_date: "2026-02-25", filer: "Nvidia", section_title: "Risk Factors", status: "current", is_current: true };

function ws(): WorkspaceContextValue {
  return { workspace: null, asOf: "", setAsOf: () => {}, setWorkspace: () => {}, generation: 0, docEvidenceEpoch: 0, invalidateDocEvidence: () => {}, headers: () => ({}) };
}

function mount(children: ReactNode) {
  return render(
    <StrictMode>
      <WorkspaceContext.Provider value={ws()}>
        <EvidenceDrawerProvider>{children}</EvidenceDrawerProvider>
      </WorkspaceContext.Provider>
    </StrictMode>,
  );
}

function probeFor(into: { current: EvidenceDrawerApi | null }) {
  return function Probe() {
    const api = useEvidenceDrawer();
    useEffect(() => {
      into.current = api;
    });
    return null;
  };
}

beforeEach(() => {
  resetChipEvidence();
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(PAYLOAD), { status: 200 })));
});
afterEach(() => vi.unstubAllGlobals());

const chipsOf = (id: string) => Array.from(document.querySelectorAll<HTMLButtonElement>(`[data-id="${id}"]`));

describe("aria-expanded on the chip whose evidence is open", () => {
  it("marks only the occurrence that was opened, and every chip points at the evidence panel", async () => {
    const user = userEvent.setup();
    mount(<AnswerText text={`a [${CHUNK}] b [${XBRL}] c [${CHUNK}]`} place="answer" gen={1} />);
    const [first, second] = chipsOf(CHUNK);
    for (const chip of [first, second, ...chipsOf(XBRL)]) {
      expect(chip).toHaveAttribute("aria-expanded", "false");
      expect(chip).toHaveAttribute("aria-controls", "evidence-panel");
    }
    await user.click(second);
    await waitFor(() => expect(second).toHaveAttribute("aria-expanded", "true"));
    expect(first).toHaveAttribute("aria-expanded", "false");
    expect(chipsOf(XBRL)[0]).toHaveAttribute("aria-expanded", "false");
    expect(document.getElementById("evidence-panel")).toHaveAttribute("aria-label", "Evidence");
  });

  it("returns to false when the drawer closes, by Escape or by Close, and focus still returns to that chip", async () => {
    const user = userEvent.setup();
    mount(<AnswerText text={`a [${CHUNK}] c [${CHUNK}]`} place="answer" gen={1} />);
    const second = chipsOf(CHUNK)[1];
    await user.click(second);
    await waitFor(() => expect(second).toHaveAttribute("aria-expanded", "true"));
    await user.keyboard("{Escape}");
    expect(second).toHaveAttribute("aria-expanded", "false");
    expect(second).toHaveFocus();
    await user.click(second);
    await user.click(screen.getByRole("button", { name: "Close evidence" }));
    expect(second).toHaveAttribute("aria-expanded", "false");
  });

  it("moves to the chip opened next while the drawer stays open", async () => {
    const user = userEvent.setup();
    mount(<AnswerText text={`a [${CHUNK}] b [${XBRL}]`} place="answer" gen={1} />);
    await user.click(chipsOf(CHUNK)[0]);
    await user.click(chipsOf(XBRL)[0]);
    await waitFor(() => expect(chipsOf(XBRL)[0]).toHaveAttribute("aria-expanded", "true"));
    expect(chipsOf(CHUNK)[0]).toHaveAttribute("aria-expanded", "false");
  });

  it("outside any provider a chip says it is not expanded and does not throw", () => {
    render(<Chip id={CHUNK} place="answer" gen={1} nth={0} />);
    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "false");
  });

  it("publishes openKey and keeps it when the drawer is opened again with no opener, and takes optional quotes", async () => {
    const api: { current: EvidenceDrawerApi | null } = { current: null };
    const Probe = probeFor(api);
    const user = userEvent.setup();
    mount(
      <>
        <AnswerText text={`a [${CHUNK}]`} place="answer" gen={1} />
        <Probe />
      </>,
    );
    expect(api.current?.openKey).toBeNull();
    await user.click(chipsOf(CHUNK)[0]);
    await waitFor(() => expect(api.current?.openKey).toBe(`answer:1:${CHUNK}:0`));
    api.current?.open(XBRL, null, "answer", ["some quoted words of a filing"]);
    await waitFor(() => expect(api.current?.openId).toBe(XBRL));
    expect(api.current?.openKey).toBe(`answer:1:${CHUNK}:0`);
    api.current?.close();
    await waitFor(() => expect(api.current?.openKey).toBeNull());
  });
});
