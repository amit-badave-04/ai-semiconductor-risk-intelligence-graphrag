import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AnswerText } from "@/components/Chips";
import { rememberChunkEvidence, resetChipEvidence } from "@/lib/chipEvidence";

// docs/specs/m5b-redesign.md RD11: chips become "source stamps". Still a <button type="button"> with data-chip-key (A1); a
// neutral surface, not solid green; an icon and a shape per kind (the icon hidden from assistive technology, the label says
// it); a government rule in the rule colours with the words "Government rule"; a stale chip in the caution colours, marked by
// shape and with words a screen reader hears (R2 review: the amber text on the muted surface was too faint).

const CHUNK = "0001045810-24-000029:I.1A:0162";
const XBRL = "xbrl:1045810:revenue:2026-01-25";
const FR = "fr:2025-00711";
const DOC = "doc:0123456789ab:v2:0007";

afterEach(() => resetChipEvidence());

const chip = (id: string) => document.querySelector(`button[data-id="${id}"]`) as HTMLElement;

describe("source stamps (RD11)", () => {
  it("labels each kind in plain words, with no pilcrow", () => {
    render(<AnswerText text={`A [${CHUNK}] B [${XBRL}] C [${FR}] D [${DOC}].`} place="answer" gen={1} />);
    expect(screen.getByRole("button", { name: "Filing 2024 \u00b7 paragraph 162" })).toHaveAttribute("data-id", CHUNK);
    expect(screen.getByRole("button", { name: "Reported revenue, period ended Jan 25, 2026" })).toHaveAttribute("data-id", XBRL);
    expect(screen.getByRole("button", { name: "Government rule 2025-00711" })).toHaveAttribute("data-id", FR);
    expect(screen.getByRole("button", { name: "Your document, version 2 \u00b7 paragraph 7" })).toHaveAttribute("data-id", DOC);
    for (const button of screen.getAllByRole("button")) {
      expect(button).toHaveAttribute("type", "button");
      expect(button).toHaveAttribute("data-chip-key");
      expect(button.textContent).not.toContain("\u00b6");
    }
  });

  it("names the filer once the chunk's evidence has loaded", () => {
    render(<AnswerText text={`A [${CHUNK}].`} place="answer" gen={1} />);
    act(() => rememberChunkEvidence(CHUNK, { filer: "Nvidia", form: "10-K", filing_date: "2024-02-21" }));
    expect(screen.getByRole("button", { name: "Nvidia 10-K, filed 2024 \u00b7 paragraph 162" })).toBeInTheDocument();
  });

  it("gives every kind its own icon, hidden from assistive technology", () => {
    render(<AnswerText text={`A [${CHUNK}] B [${XBRL}] C [${FR}] D [${DOC}].`} place="answer" gen={1} />);
    const icons = [CHUNK, XBRL, FR, DOC].map((id) => chip(id).querySelector("svg"));
    for (const icon of icons) expect(icon).toHaveAttribute("aria-hidden", "true");
    expect(new Set(icons.map((icon) => icon?.getAttribute("class"))).size).toBe(4);
  });

  it("puts a filing on the neutral surface and a rule in the rule colours (never solid green)", () => {
    render(<AnswerText text={`A [${CHUNK}] C [${FR}].`} place="answer" gen={1} />);
    expect(chip(CHUNK).className).toMatch(/\bbg-card\b/);
    expect(chip(CHUNK).className).toMatch(/\bborder-border-strong\b/);
    expect(chip(CHUNK).className).not.toMatch(/\bbg-primary\b/);
    expect(chip(FR).className).toMatch(/\bbg-rule-fill\b/);
    expect(chip(FR).className).toMatch(/\btext-rule\b/);
  });

  it("marks a stale chip in the caution colours, by shape, and in visible words", () => {
    render(<AnswerText text={`A [${DOC}].`} place="answer" gen={1} staleIds={[DOC]} />);
    const stale = chip(DOC);
    expect(stale).toHaveAttribute("data-stale");
    expect(stale.className).toMatch(/\btext-caution\b/);
    expect(stale.className).toMatch(/\bborder-caution-line\b/);
    expect(stale.className).not.toMatch(/amber/);
    expect(stale.querySelector("svg.lucide-triangle-alert")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Your document, version 2 \u00b7 paragraph 7 (older version)" })).toBe(stale);
  });
});
