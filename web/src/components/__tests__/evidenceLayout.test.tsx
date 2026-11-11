import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AnswerText } from "@/components/Chips";
import { EvidenceDrawerProvider } from "@/components/EvidenceDrawer";
import { resetChipEvidence } from "@/lib/chipEvidence";
import { WorkspaceContext, type WorkspaceContextValue } from "@/state/useWorkspace";

// docs/specs/m5b-redesign.md RD16: the same single <aside aria-label="Evidence"> and controller (A5 unchanged), laid out as a
// column beside the page from 900 px and as a bottom sheet below that. While it is open the root element carries data-evidence,
// which the stylesheet uses to make room for it (the page is padded by its width or its height, WCAG 2.4.11); the chip that
// opened it is scrolled into view; its header holds a visible "Close" button at least 44 px tall, still named "Close evidence".

const CHUNK = "0001045810-24-000029:I.1A:0162";

function ws(): WorkspaceContextValue {
  return { workspace: null, asOf: "", setAsOf: () => {}, setWorkspace: () => {}, generation: 0, docEvidenceEpoch: 0, invalidateDocEvidence: () => {}, headers: () => ({}) };
}

function mount() {
  return render(
    <WorkspaceContext.Provider value={ws()}>
      <EvidenceDrawerProvider>
        <AnswerText text={`See [${CHUNK}].`} place="answer" gen={1} />
      </EvidenceDrawerProvider>
    </WorkspaceContext.Provider>,
  );
}

const root = () => document.documentElement;
const panel = () => document.querySelector("aside[aria-label='Evidence']") as HTMLElement;
const chip = () => document.querySelector(`button[data-id="${CHUNK}"]`) as HTMLElement;

beforeEach(() => {
  resetChipEvidence();
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
});
afterEach(() => {
  vi.unstubAllGlobals();
  delete (Element.prototype as Partial<Element>).scrollIntoView; // jsdom has none: only the test that needs it defines one
});

describe("the evidence panel's layout (RD16)", () => {
  it("marks the root element while it is open, and only then", async () => {
    const user = userEvent.setup();
    mount();
    expect(root()).not.toHaveAttribute("data-evidence");
    await user.click(chip());
    expect(root()).toHaveAttribute("data-evidence");
    await user.keyboard("{Escape}");
    // N1 (the owner's NVDA pass): a close that gives focus back finishes one frame later, so these wait for it.
    await waitFor(() => expect(root()).not.toHaveAttribute("data-evidence"));
  });

  it("clears the mark when the page unmounts with the panel open", async () => {
    const user = userEvent.setup();
    const { unmount } = mount();
    await user.click(chip());
    expect(root()).toHaveAttribute("data-evidence");
    unmount();
    expect(root()).not.toHaveAttribute("data-evidence");
  });

  it("scrolls the chip that opened it into view, the nearest way", async () => {
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    const user = userEvent.setup();
    mount();
    await user.click(chip());
    expect(scroll).toHaveBeenCalledWith({ block: "nearest", inline: "nearest" });
    expect(scroll.mock.contexts.at(-1)).toBe(chip());
  });

  it("has a visible Close of at least 44 px in a header that does not scroll with the evidence, still named Close evidence", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(chip());
    const aside = screen.getByRole("complementary", { name: "Evidence" });
    const close = within(aside).getByRole("button", { name: "Close evidence" });
    expect(close).toHaveTextContent("Close");
    expect(close.className).toMatch(/min-h-\[44px\]/); // 44 CSS px: min-h-11 is 2.75rem, 41.25 px at the 15 px root
    const body = aside.querySelector("[data-evidence-body]") as HTMLElement;
    expect(body).not.toBeNull();
    expect(body.className).toMatch(/\boverflow-auto\b/);
    expect(body.contains(close)).toBe(false);
    act(() => close.click());
    await waitFor(() => expect(chip()).toHaveFocus());
  });

  it("is a bottom sheet below 900 px and a column from 900 px, no longer offset under the header", () => {
    mount();
    const classes = panel().className;
    expect(classes).not.toMatch(/lg:top-14/);
    expect(classes).toMatch(/\bbottom-0\b/);
    expect(classes).toMatch(/h-\[var\(--evidence-sheet\)\]/);
    expect(classes).toMatch(/min-\[900px\]:w-\[var\(--evidence-column\)\]/);
  });
});
