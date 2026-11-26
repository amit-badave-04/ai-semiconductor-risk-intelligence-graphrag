import { describe, expect, it } from "vitest";
import { deck } from "@/copy/deck";

// The upload card's words, as the llm-council settled them (M2 I4b): one term pair, "keyword search" and "search by meaning".
const C = deck.jobs.card;

describe("upload card copy: the council's wording", () => {
  it("the fixed strings", () => {
    expect(deck.jobs.keywordReady).toBe("Keyword search ready");
    expect(C.preflightHeading).toBe("What we found in this file");
    expect(C.passagesHelp).toBe("A passage is a short piece of your file.");
    expect(C.noSections).toBe("No standard sections found.");
    expect(C.keywordReady).toBe("Keyword search is ready. You can ask questions now.");
    expect(C.waiting).toBe("Search by meaning is waiting to start. Keyword search works now.");
    expect(C.started).toBe("Preparing search by meaning started.");
    expect(C.progressName).toBe("Preparing search by meaning");
    expect(C.cancel).toBe("Stop preparing search by meaning");
    expect(C.cancelHelp).toBe("Keyword search keeps working. Passages already prepared stay searchable by meaning.");
    expect(C.stopping).toBe("Stopping\u2026");
    expect(C.waitUnknown).toBe("Search by meaning: time estimate shown once it starts. This can take up to an hour.");
  });

  it("the form line says what still works", () => {
    expect(C.form("10-K")).toBe("Form: 10-K");
    expect(C.form(null)).toBe("Form: not recognised. You can still ask about this file.");
  });

  it("passages and progress have singular and plural forms", () => {
    expect(C.passages(1)).toBe("1 passage to search");
    expect(C.passages(12)).toBe("12 passages to search");
    expect(C.progress(6, 12)).toBe("Preparing search by meaning: 6 of 12 passages");
    expect(C.progress(0, 1)).toBe("Preparing search by meaning: 0 of 1 passage");
  });

  it("the time left is an estimate in minutes, never seconds", () => {
    const wait = (seconds: number): string => C.wait(seconds);
    expect(wait(0.4)).toBe("Search by meaning: less than a minute left (estimate)");
    expect(wait(59)).toBe("Search by meaning: less than a minute left (estimate)");
    expect(wait(60)).toBe("Search by meaning: about 1 minute left (estimate)");
    expect(wait(61)).toBe("Search by meaning: about 2 minutes left (estimate)");
    expect(wait(600)).toBe("Search by meaning: about 10 minutes left (estimate)");
    expect(wait(601)).toBe("Search by meaning: about 10 minutes left (estimate)"); // 11 minutes, above 10: nearest 5
    expect(wait(14 * 60)).toBe("Search by meaning: about 15 minutes left (estimate)");
    expect(wait(12 * 60)).toBe("Search by meaning: about 10 minutes left (estimate)");
    expect(wait(13 * 60)).toBe("Search by meaning: about 15 minutes left (estimate)");
    expect(wait(47 * 60)).toBe("Search by meaning: about 45 minutes left (estimate)");
    expect(wait(58 * 60)).toBe("Search by meaning: about 1 hour left (estimate)"); // rounds to 60
    expect(wait(59 * 60)).toBe("Search by meaning: about 1 hour left (estimate)");
    expect(wait(60 * 60)).toBe("Search by meaning: about 1 hour left (estimate)");
    expect(wait(3400)).toBe("Search by meaning: about 55 minutes left (estimate)");
    expect(wait(5 * 3600)).toBe("Search by meaning: about 1 hour left (estimate)");
    for (const s of [0.4, 59, 61, 3400, 7200]) expect(wait(s)).not.toMatch(/\d\s?s\b/);
  });

  it("the end lines", () => {
    expect(C.summary(12, 0, null)).toBe("All 12 passages can be searched by keyword and by meaning.");
    expect(C.summary(1, 0, null)).toBe("The 1 passage can be searched by keyword and by meaning.");
    expect(C.summary(30, 10, "timeout")).toBe("Search by meaning stopped early. 30 passages can be searched by keyword and by meaning. 10 can be searched by keyword only.");
    expect(C.summary(30, 1, null)).toBe("30 passages can be searched by keyword and by meaning. 1 can be searched by keyword only.");
    expect(C.summary(1, 5, null)).toBe("1 passage can be searched by keyword and by meaning. 5 can be searched by keyword only.");
    expect(C.summary(0, 7, null)).toBe("All 7 passages can be searched by keyword only.");
    expect(C.summary(0, 1, null)).toBe("The 1 passage can be searched by keyword only.");
    expect(C.summary(0, 0, null)).toBe("No text was found to search in this file.");
  });

  it("the stopped lines: by the user, and by itself", () => {
    expect(C.summary(3, 9, "cancelled")).toBe("Stopped. 3 passages can be searched by keyword and by meaning. 9 can be searched by keyword only.");
    expect(C.summary(0, 12, "cancelled")).toBe("Stopped. All 12 passages can be searched by keyword only.");
    for (const why of ["timeout", "budget", "guaranteed", "embed_error", "daily_limit", "superseded"]) {
      expect(C.summary(0, 12, why)).toBe("Search by meaning stopped early. All 12 passages can be searched by keyword only.");
    }
    // nothing was left over: the job was stopped but every passage was prepared, so there is nothing to apologise for
    expect(C.summary(12, 0, "cancelled")).toBe("All 12 passages can be searched by keyword and by meaning.");
  });

  it("the card never uses the words the council banned", () => {
    const text = JSON.stringify([
      C.preflightHeading, C.passagesHelp, C.form(null), C.passages(2), C.noSections, C.wait(30), C.waitUnknown,
      C.keywordReady, C.waiting, C.started, C.progress(1, 2), C.progressName, C.cancel, C.cancelHelp, C.stopping,
      C.summary(1, 1, "timeout"), C.summary(0, 0, null), deck.jobs.keywordReady,
      deck.checked.line({ code: "uploadKeywordOnly", tone: "plain" }),
      deck.checked.line({ code: "uploadKeywordSemantic", tone: "plain" }),
      deck.checked.line({ code: "uploadKeywordSemanticPartial", tone: "plain" }),
    ]);
    expect(text).not.toMatch(/semantic|index|hybrid|state of the art/i);
  });
});
