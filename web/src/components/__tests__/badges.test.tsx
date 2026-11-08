import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AnswerMeta } from "@/components/Badges";
import { answerBadge, checksPassed, checksSummary, escalationStatus, metaSummary, reasonText, type Badge } from "@/lib/answerMeta";

// tests/ui/badges.test.mjs (15 titles) and tests/ui/checks.test.mjs (17 of its 19; the 2 example-panel titles are ported with the
// Ask page in pages/__tests__/ask-page.test.tsx), ported. Each title is the legacy title, character for character. The pure
// functions run as the shipped lib/answerMeta.ts; the legacy `metaHtml` titles run through the AnswerMeta component.
const LUNA = "openai/gpt-6-luna";
const SONNET = "anthropic/claude-sonnet-5";
const CLEAN = {
  citations_retrieved: true,
  numbers_grounded: true,
  numbers_checked: 2,
  unmatched_numbers: [],
  echoed_numbers: [],
  pseudo_citations: [],
  has_citation: true,
  is_refusal: false,
  unsupported_removal_claim: false,
  unsupported_removal_sentences: [],
};
const summary = (checks: unknown, extra: Record<string, unknown> = {}) => checksSummary({ citations: ["a"], hallucinated: [], checks, ...extra });
const texts = (s: { badges: Badge[] }) => s.badges.map((b) => b.text);
const badge = (s: { badges: Badge[] }, re: RegExp) => s.badges.find((b) => re.test(b.text));
const meta = (event: Record<string, unknown>) => metaSummary(event);
const shown = (event: Record<string, unknown>) => {
  const { container } = render(<AnswerMeta done={event} />);
  return container;
};

describe("tests/ui/badges.test.mjs", () => {
  it("a routed answer is never described as a verified draft", () => {
    const b = answerBadge({ routed: "strong", escalated: false, answered_by: SONNET });
    expect(b?.text).toMatch(/answered by claude-sonnet-5/);
    expect(b?.text).toMatch(/routed: change-over-time question; no cheap draft/);
    expect(b?.text).not.toMatch(/draft (was )?verified|draft passed/i);
  });

  it("an escalated answer names the failed checks in plain words", () => {
    const b = answerBadge({ routed: "cheap", escalated: true, answered_by: SONNET, escalation_reasons: ["ungrounded_number", "invalid_citation"] });
    expect(b?.text).toMatch(/answered by claude-sonnet-5/);
    expect(b?.text).toMatch(/escalated after a failed check/);
    expect(b?.text).toMatch(/number not found in the retrieved context/);
    expect(b?.text).toMatch(/cited id was not in the retrieved context/);
  });

  it("an unknown escalation reason falls back to a readable form", () => {
    expect(reasonText("some_new_reason")).toBe("some new reason");
  });

  it("a cheap draft that passed says so, and only when no check flagged anything", () => {
    const ok = answerBadge({
      routed: "cheap",
      escalated: false,
      answered_by: LUNA,
      checks: { citations_retrieved: true, numbers_grounded: true, unmatched_numbers: [], pseudo_citations: [] },
    });
    expect(ok?.text).toBe("gpt-6-luna draft passed the checks");
    const flagged = answerBadge({
      routed: "cheap",
      escalated: false,
      answered_by: LUNA,
      checks: { citations_retrieved: true, numbers_grounded: false, unmatched_numbers: ["$9.9B"], pseudo_citations: [] },
    });
    expect(flagged?.text).not.toMatch(/passed the checks/);
    expect(flagged?.cls).toBe("warn");
  });

  it("with the routing fields missing the badge claims nothing about verification", () => {
    expect(answerBadge({ answered_by: LUNA })?.text).toBe("answered by gpt-6-luna");
  });

  it("a live stream without escalation falls back to the configured answer model", () => {
    expect(answerBadge({ cached: false }, SONNET)?.text).toBe("answered by claude-sonnet-5 (streamed live)");
    expect(answerBadge({ cached: false })).toBeNull();
    expect(answerBadge(null)).toBeNull();
  });

  // legacy title: no badge text anywhere says the two withdrawn phrases about a verified draft and verified citations; the S15 scan
  // of web/src forbids those phrases in any file, tests included, so the title names them instead of quoting them.
  it("no badge text anywhere claims a verified draft or verified citations (legacy title: the two withdrawn phrases)", () => {
    const events = [
      { routed: "strong", answered_by: SONNET },
      { routed: "cheap", escalated: true, answered_by: SONNET, escalation_reasons: ["no_citation"] },
      { routed: "cheap", escalated: false, answered_by: LUNA, checks: { citations_retrieved: true, numbers_grounded: true } },
      { answered_by: LUNA },
    ];
    for (const ev of events) {
      const text = shown({ ...ev, citations: ["a"], hallucinated: [], usage: {}, cost_usd: 0.001 }).textContent ?? "";
      expect(text).not.toMatch(new RegExp(["draft", "verified"].join(" "), "i"));
      expect(text).not.toMatch(new RegExp(["all citations", "verified"].join(" "), "i"));
    }
  });

  it("checks that all passed produce honest wording and no warnings", () => {
    // green "numbers matched" needs numbers_checked > 0 (the review): a figure-free answer has nothing to match
    const s = checksSummary({
      citations: ["x", "y"],
      hallucinated: [],
      checks: {
        citations_retrieved: true,
        numbers_grounded: true,
        numbers_checked: 2,
        unmatched_numbers: [],
        pseudo_citations: [],
        has_citation: true,
        is_refusal: false,
        echoed_numbers: [],
        unsupported_removal_claim: false,
      },
    });
    const t = texts(s);
    expect(t).toContain("2 citations");
    expect(t).toContain("cited ids were retrieved");
    expect(t).toContain("numbers matched the retrieved context");
    expect(s.warnings).toEqual([]);
    expect(t.every((x) => !/verified/i.test(x))).toBe(true);
    expect(s.hint).toMatch(/do not prove/);
  });

  it("unmatched numbers and pseudo-citations become visible warnings", () => {
    const s = checksSummary({
      citations: ["x"],
      hallucinated: [],
      checks: { citations_retrieved: true, numbers_grounded: false, unmatched_numbers: ["$12.9B", "43%"], pseudo_citations: ["[Reported Metrics]"] },
    });
    expect(s.badges.some((b) => b.cls === "bad" && /numbers/.test(b.text))).toBe(true);
    const all = s.warnings.join("\n");
    expect(all).toMatch(/\$12\.9B/);
    expect(all).toMatch(/43%/);
    expect(all).toMatch(/\[Reported Metrics\]/);
  });

  it("a cited id outside the retrieved context is flagged as bad", () => {
    const s = checksSummary({ citations: ["a", "b"], hallucinated: ["b"], checks: { citations_retrieved: false } });
    expect(s.badges.some((b) => b.cls === "bad" && /not in the retrieved context/.test(b.text))).toBe(true);
    expect(s.warnings.join("\n")).toMatch(/b/);
  });

  it("answers without a checks object (cached, older) do not claim their numbers were checked", () => {
    const t = texts(checksSummary({ citations: ["a"], hallucinated: [] }));
    expect(t).toContain("cited ids were retrieved");
    expect(t).toContain("numbers not checked");
    expect(t.some((x) => /matched the retrieved context/.test(x))).toBe(false);
  });

  it("a missing or malformed event never throws", () => {
    expect(() => checksSummary({})).not.toThrow();
    expect(() => checksSummary({ citations: "nope", hallucinated: null, checks: "x" })).not.toThrow();
    expect(() => shown({})).not.toThrow();
    expect(() => shown({ usage: null, cost_usd: "free", citations: null })).not.toThrow();
    expect(() => metaSummary(undefined)).not.toThrow();
  });

  it("server-supplied strings are escaped inside the badges and warnings", () => {
    const c = shown({
      citations: [],
      hallucinated: [],
      routed: "cheap",
      answered_by: "x/<img src=x onerror=alert(1)>",
      checks: { unmatched_numbers: ["<script>alert(1)</script>"], pseudo_citations: ['"><b>x</b>'] },
    });
    expect(c.querySelector("script")).toBeNull();
    expect(c.querySelector("img")).toBeNull();
    expect(c.querySelector("b")).toBeNull();
    expect(c.textContent).toContain("<script>alert(1)</script>");
  });

  it("a truncated live answer is flagged, a cached one says it was saved", () => {
    expect(texts(meta({ finish_reason: "length", citations: [] })).join("|")).toMatch(/truncated by the token budget/);
    const cached = texts(meta({ cached: true, source: "benchmark", citations: [] })).join("|");
    expect(cached).toMatch(/saved example answer/);
    expect(cached).not.toMatch(/benchmarked answer/);
  });

  it("the escalation status names the reasons the draft was rejected", () => {
    const s = escalationStatus({ reasons: ["ungrounded_number", "draft_error"], from: LUNA, to: SONNET });
    expect(s).toMatch(/did not pass the automatic checks/);
    expect(s).toMatch(/number not found in the retrieved context/);
    expect(s).toMatch(/draft model failed/);
    expect(escalationStatus({})).not.toMatch(/undefined|\(\)/);
  });
});

describe("tests/ui/checks.test.mjs", () => {
  it("with figures examined and all grounded the numbers badge is green", () => {
    expect(badge(summary(CLEAN), /numbers matched the retrieved context/)?.cls).toBe("ok");
  });

  it("with no figure to check the badge is neutral and never green", () => {
    const s = summary({ ...CLEAN, numbers_checked: 0 });
    const b = badge(s, /no figures to check/);
    expect(b, texts(s).join(" | ")).toBeDefined();
    expect(b?.cls).toBe("");
    expect(texts(s).some((t) => /matched the retrieved context/.test(t))).toBe(false);
  });

  it("an old payload without numbers_checked still reads 'numbers not checked', not 'no figures to check'", () => {
    const { numbers_checked: _omitted, ...old } = CLEAN;
    const s = summary(old);
    expect(texts(s)).toContain("numbers not checked");
    expect(texts(s).some((t) => /matched the retrieved context|no figures to check/.test(t))).toBe(false);
    expect(texts(summary(undefined))).toContain("numbers not checked");
  });

  it("a figure taken from the question is a warning, not a match", () => {
    const s = summary({ ...CLEAN, numbers_grounded: false, echoed_numbers: ["$500 billion"] });
    expect(texts(s).some((t) => /matched the retrieved context/.test(t))).toBe(false);
    expect(badge(s, /taken from your question/)?.cls).toBe("warn");
    expect(s.warnings.join("\n")).toMatch(/A figure taken from your question, not verified: \$500 billion/);
  });

  it("echoed and unmatched figures are reported separately", () => {
    const s = summary({ ...CLEAN, numbers_grounded: false, echoed_numbers: ["$500 billion"], unmatched_numbers: ["$190 billion"] });
    const all = s.warnings.join("\n");
    expect(all).toMatch(/Numbers not found in the retrieved context: \$190 billion/);
    expect(all).toMatch(/taken from your question, not verified: \$500 billion/);
    expect(s.badges.some((b) => b.cls === "bad" && /numbers/.test(b.text))).toBe(true);
  });

  it("an answer with no citation that is not a refusal never shows the green cited-ids badge", () => {
    const s = summary({ ...CLEAN, has_citation: false, numbers_checked: 0 }, { citations: [] });
    expect(texts(s)).not.toContain("cited ids were retrieved");
    expect(badge(s, /no citation/i)?.cls, texts(s).join(" | ")).toBe("warn");
    expect(s.warnings.join("\n")).toMatch(/cites no source/i);
  });

  it("a zero-citation refusal is fine: neutral, no warning, and not green either", () => {
    const s = summary({ ...CLEAN, has_citation: false, is_refusal: true, numbers_checked: 0 }, { citations: [] });
    expect(texts(s)).not.toContain("cited ids were retrieved");
    expect(s.badges.every((b) => b.cls !== "warn" && b.cls !== "bad"), texts(s).join(" | ")).toBe(true);
    expect(s.warnings).toEqual([]);
  });

  it("a payload with zero citations and no checks at all is never green about citations", () => {
    expect(texts(checksSummary({ citations: [], hallucinated: [] }))).not.toContain("cited ids were retrieved");
  });

  it("cited answers keep the green cited-ids badge", () => {
    expect(texts(summary(CLEAN))).toContain("cited ids were retrieved");
  });

  it("an unsupported removal claim is a warning badge and names the sentence", () => {
    const s = summary({ ...CLEAN, unsupported_removal_claim: true, unsupported_removal_sentences: ["Nvidia dropped its export-control risk."] });
    const b = badge(s, /removal/i);
    expect(b?.cls).toBe("warn");
    expect(s.warnings.join("\n")).toMatch(/"no longer appears" lists/);
    expect(s.warnings.join("\n")).toMatch(/Nvidia dropped its export-control risk\./);
    expect(b?.text).not.toMatch(/removed lists/);
  });

  it("the checks hint points at the change-claim reliability note and never calls the comparison verified", () => {
    const s = summary(CLEAN);
    expect(s.hint).toMatch(/automatic text comparison/);
    expect(s.hint).toMatch(/How reliable are change claims\?/);
    expect(s.hint).not.toMatch(new RegExp(["text-verified", ["verified", "absent"].join(" ")].join("|")));
  });

  it("a removal claim flag without sentences still warns", () => {
    expect(badge(summary({ ...CLEAN, unsupported_removal_claim: true }), /removal/i)).toBeDefined();
  });

  it("a cheap draft released with any of the new failures is not called a pass", () => {
    for (const failing of [{ has_citation: false }, { echoed_numbers: ["$5"], numbers_grounded: false }, { unsupported_removal_claim: true }]) {
      const b = answerBadge({ routed: "cheap", escalated: false, answered_by: SONNET, checks: { ...CLEAN, ...failing } });
      expect(b?.text).not.toMatch(/passed the checks/);
      expect(b?.cls).toBe("warn");
    }
    expect(answerBadge({ routed: "cheap", escalated: false, answered_by: SONNET, checks: CLEAN })?.text).toBe("claude-sonnet-5 draft passed the checks");
  });

  it("checksPassed is one predicate: a refusal with no citation passes, an uncited claim does not", () => {
    expect(checksPassed(CLEAN)).toBe(true);
    expect(checksPassed({ ...CLEAN, has_citation: false, is_refusal: true })).toBe(true);
    expect(checksPassed({ ...CLEAN, has_citation: false, is_refusal: false })).toBe(false);
    expect(checksPassed({ ...CLEAN, echoed_numbers: ["$1"] })).toBe(false);
    expect(checksPassed({ ...CLEAN, unsupported_removal_claim: true })).toBe(false);
    expect(checksPassed({ citations_retrieved: true })).toBe(true); // an older payload: nothing it predates is failed
    expect(checksPassed(undefined)).toBe(true);
  });

  it("the new escalation reasons read in plain words", () => {
    expect(reasonText("unsupported_removal_claim")).toMatch(/removal claim/);
    expect(reasonText("unsupported_removal_claim")).toMatch(/"no longer appears" lists/);
    expect(reasonText("ungrounded_number")).toMatch(/number not found in the retrieved context/);
    expect(escalationStatus({ reasons: ["unsupported_removal_claim"] })).toMatch(/removal claim/);
  });

  it("sentences and figures from the server are escaped in the new warnings", () => {
    const c = shown({
      citations: [],
      hallucinated: [],
      checks: {
        ...CLEAN,
        has_citation: false,
        numbers_grounded: false,
        echoed_numbers: ["<img src=x onerror=alert(1)>"],
        unsupported_removal_claim: true,
        unsupported_removal_sentences: ["<script>alert(2)</script> was removed"],
      },
    });
    expect(c.querySelector("script")).toBeNull();
    expect(c.querySelector("img")).toBeNull();
    expect(c.textContent).toContain("<script>alert(2)</script> was removed");
  });

  it("the hint says which figures are checked and that a bare number is not", () => {
    const s = summary(CLEAN);
    expect(s.hint).toMatch(/do not prove/);
    expect(s.hint).toMatch(/currency/);
    expect(s.hint).toMatch(/bare number/);
    expect(s.hint).toMatch(/only your question states/);
  });
});
