import { readFileSync } from "node:fs";
import path from "node:path";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetSpecimenCache } from "@/lib/specimenCache";
import { SPECIMENS } from "@/lib/specimens";
import { SpecimenStamp } from "../SpecimenStamp";

// docs/specs/m5b-redesign.md RD4, SR3, SR9: a specimen stamp is a button that opens a click popover with a real example (the
// evidence recorded from the live service); the id passes classifyId and is fetched once; one Escape closes the popover and
// nothing else, whether focus is inside the popover or not.

const recorded = JSON.parse(readFileSync(path.resolve(import.meta.dirname, "../../test/fixtures/specimens.recorded.json"), "utf-8")) as {
  payloads: Record<string, unknown>;
};

let calls: string[] = [];

beforeEach(() => {
  resetSpecimenCache();
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      calls.push(String(url));
      const id = decodeURIComponent(String(url).replace("/api/evidence/", ""));
      const payload = recorded.payloads[id];
      return payload === undefined ? new Response("{}", { status: 404 }) : new Response(JSON.stringify(payload), { status: 200 });
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

describe("SpecimenStamp", () => {
  it("is a button named by its kind, and opening it shows the recorded example from one evidence request (SR3)", async () => {
    const user = userEvent.setup();
    render(<SpecimenStamp kind="number" />);
    const stamp = screen.getByRole("button", { name: "reported number" });
    expect(calls).toEqual([]); // nothing is fetched before the stamp is opened
    await user.click(stamp);
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(dialog).toHaveTextContent("Nvidia revenue, FY2026 (ended Jan 25, 2026)"));
    expect(dialog).toHaveTextContent("$215.9B");
    expect(dialog).toHaveTextContent("Exactly $215,938,000,000");
    expect(calls).toEqual([`/api/evidence/${encodeURIComponent(SPECIMENS.number)}`]);
  });

  it("shows the government rule as the government's, with its label and the keyword-matching note", async () => {
    const user = userEvent.setup();
    render(<SpecimenStamp kind="rule" />);
    await user.click(screen.getByRole("button", { name: "government rule" }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(dialog).toHaveTextContent("Government rule"));
    expect(dialog).toHaveTextContent(/not statements the company made/);
    const link = screen.getByRole("link", { name: "Open the rule on FederalRegister.gov" });
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("moves focus into the popover, and Escape there closes it, returns focus to the stamp and is marked handled (SR9)", async () => {
    const user = userEvent.setup();
    render(<SpecimenStamp kind="passage" />);
    const stamp = screen.getByRole("button", { name: "filing passage" });
    await user.click(stamp);
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    const seen: boolean[] = [];
    const drawerLike = (event: KeyboardEvent) => seen.push(event.defaultPrevented);
    document.addEventListener("keydown", drawerLike);
    await user.keyboard("{Escape}");
    document.removeEventListener("keydown", drawerLike);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(stamp).toHaveFocus();
    expect(seen).not.toContain(false); // a listener on the document (the evidence drawer's) never sees it unhandled (Base UI stops it here)
  });

  // Focus outside the popup but still on the popover (its stamp): Base UI keeps it open there. Focus moved anywhere else closes a
  // non-modal popover, and the next Escape then rightly belongs to whatever is open below it.
  it("with focus outside the popup (on its stamp), one Escape still closes only the popover (SR9)", async () => {
    const user = userEvent.setup();
    render(<SpecimenStamp kind="passage" />);
    const stamp = screen.getByRole("button", { name: "filing passage" });
    await user.click(stamp);
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(dialog).toHaveTextContent("Nvidia 10-K")); // the example has loaded, as a reader would see it
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true)); // Base UI has moved focus in (it does so late)
    stamp.focus();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(stamp).toHaveAttribute("aria-expanded", "true");
    expect(dialog.contains(document.activeElement)).toBe(false);
    const seen: boolean[] = [];
    const drawerLike = (event: KeyboardEvent) => seen.push(event.defaultPrevented);
    document.addEventListener("keydown", drawerLike);
    await user.keyboard("{Escape}");
    document.removeEventListener("keydown", drawerLike);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(seen).not.toContain(false); // the evidence drawer's listener never sees it unhandled
  });

  it("says plainly when the example cannot be loaded", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 503 })));
    const user = userEvent.setup();
    render(<SpecimenStamp kind="number" />);
    await user.click(screen.getByRole("button", { name: "reported number" }));
    expect(await screen.findByText("The example could not be loaded right now.")).toBeInTheDocument();
  });
});
