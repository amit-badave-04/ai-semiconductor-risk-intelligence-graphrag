// R22 (docs/specs/m5b-frontend.md): the badges and the checks of an answer, as DATA (a list of badges, a list of warnings, one hint).
// A port of index.html's reasonText, escalationStatus, checksPassed, answerBadge, checksSummary and metaHtml. The wording is the legacy
// wording, character for character (pinned by tests/test_web_ask_copy.py and the differential test), and a badge never claims more
// than the checks prove. Every value from the server is a plain string here; the components render it as a text node (S2, S11).
import { CHECKS_HINT } from "@/pages/Method.copy";

export type BadgeClass = "" | "ok" | "warn" | "bad";
export interface Badge {
  text: string;
  cls: BadgeClass;
}
export interface ChecksSummary {
  badges: Badge[];
  warnings: string[];
  hint: string;
}

type Dict = Record<string, unknown>;
const isDict = (value: unknown): value is Dict => typeof value === "object" && value !== null;
const dictOf = (value: unknown): Dict => (isDict(value) ? value : {});
const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
const text = (value: unknown): string => (value === null || value === undefined ? "" : String(value));

const REASONS: Readonly<Record<string, string>> = {
  empty: "empty answer",
  truncated: "answer cut off by the token budget",
  invalid_citation: "a cited id was not in the retrieved context",
  no_citation: "no citation",
  ungrounded_number: "a number not found in the retrieved context (or stated only in your question)",
  pseudo_citation: "bracketed text that is not a citation",
  unsupported_removal_claim: 'a removal claim that the retrieved "no longer appears" lists do not support',
  draft_error: "the draft model failed",
};

/** The last path segment of a model name (`anthropic/claude-sonnet-5` is `claude-sonnet-5`). */
export const shortModel = (model: unknown): string => text(model).split("/").pop() ?? "";

/** A failed check's reason in plain words; an unknown one reads as its name with spaces. */
export function reasonText(reason: unknown): string {
  const key = text(reason);
  return Object.hasOwn(REASONS, key) ? REASONS[key] : key.replace(/_/g, " ");
}

/** The status line while a rejected draft is re-answered (the `escalated` event carries the reasons). */
export function escalationStatus(event: unknown): string {
  const why = list(dictOf(event).reasons).map(reasonText);
  return `the first draft did not pass the automatic checks${why.length ? ` (${why.join("; ")})` : ""}; answering again with the stronger model\u2026`;
}

/**
 * One predicate for "this answer failed a check" (the server's failed_check_names is the same rule): an unretrieved citation, an
 * ungrounded or question-echoed figure, a pseudo-citation, an unsupported removal claim, or no citation at all in an answer that
 * is not a refusal. A key an older payload does not carry counts as clean.
 */
export function checksPassed(checks: unknown): boolean {
  if (!isDict(checks)) return true;
  return !(
    checks.citations_retrieved === false ||
    checks.numbers_grounded === false ||
    list(checks.unmatched_numbers).length > 0 ||
    list(checks.echoed_numbers).length > 0 ||
    list(checks.pseudo_citations).length > 0 ||
    checks.unsupported_removal_claim === true ||
    (checks.has_citation === false && checks.is_refusal !== true)
  );
}

/** What actually happened to this answer, and nothing more. `defaultModel` (from /api/stats) names the model of a live stream. */
export function answerBadge(event: unknown, defaultModel?: unknown): Badge | null {
  if (!isDict(event)) return null;
  if (!event.answered_by) {
    if (event.cached || !defaultModel || event.routed || event.escalated) return null;
    return { text: `answered by ${shortModel(defaultModel)} (streamed live)`, cls: "" };
  }
  const model = shortModel(event.answered_by);
  if (event.routed === "strong") return { text: `answered by ${model} (routed: change-over-time question; no cheap draft)`, cls: "" };
  if (event.escalated) {
    const why = list(event.escalation_reasons).map(reasonText);
    return { text: `answered by ${model}, escalated after a failed check${why.length ? ` (${why.join("; ")})` : ""}`, cls: "" };
  }
  if (event.routed === "cheap") {
    return checksPassed(event.checks)
      ? { text: `${model} draft passed the checks`, cls: "" }
      : { text: `${model} draft released with check warnings (see below)`, cls: "warn" };
  }
  return { text: `answered by ${model}`, cls: "" };
}

/** The citation badge: green only when something was cited and every cited id was retrieved; zero citations is never green. */
function citationBadges(cites: unknown[], hall: unknown[], c: Dict | null, out: ChecksSummary): void {
  const uncited = c !== null && c.has_citation === false && c.is_refusal !== true;
  if (hall.length || (c !== null && c.citations_retrieved === false)) {
    out.badges.push({ text: hall.length ? `${hall.length} cited id(s) not in the retrieved context` : "some cited ids were not in the retrieved context", cls: "bad" });
    if (hall.length) out.warnings.push(`Cited but not retrieved: ${hall.join(", ")}`);
  } else if (uncited) {
    out.badges.push({ text: "no citation: this answer cites no source", cls: "warn" });
    out.warnings.push("This answer cites no source and is not a refusal: read it as unsupported.");
  } else if (c !== null && c.has_citation === false && c.is_refusal === true) {
    out.badges.push({ text: "declines to answer: nothing to cite", cls: "" });
  } else if (cites.length) {
    out.badges.push({ text: "cited ids were retrieved", cls: "ok" });
  }
}

/** The numbers badges: green only when figures were examined and all matched; a figure only the question states is never a match. */
function numberBadges(c: Dict | null, unmatched: unknown[], echoed: unknown[], out: ChecksSummary): void {
  if (c === null) out.badges.push({ text: "numbers not checked", cls: "" });
  else if (unmatched.length || (c.numbers_grounded === false && !echoed.length)) out.badges.push({ text: "some numbers are not in the retrieved context", cls: "bad" });
  if (c !== null && echoed.length) out.badges.push({ text: "a figure was taken from your question and is not verified", cls: "warn" });
  if (c !== null && !unmatched.length && !echoed.length && c.numbers_grounded !== false) {
    if (typeof c.numbers_checked !== "number") out.badges.push({ text: "numbers not checked", cls: "" });
    else if (c.numbers_checked > 0) out.badges.push({ text: "numbers matched the retrieved context", cls: "ok" });
    else out.badges.push({ text: "no figures to check", cls: "" });
  }
}

function warningLines(c: Dict | null, unmatched: unknown[], echoed: unknown[], out: ChecksSummary): void {
  if (unmatched.length) out.warnings.push(`Numbers not found in the retrieved context: ${unmatched.join(", ")}`);
  if (echoed.length) out.warnings.push(`A figure taken from your question, not verified: ${echoed.join(", ")}`);
  if (c === null) return;
  if (list(c.pseudo_citations).length) out.warnings.push(`Bracketed text that is not a citation and cannot be verified: ${list(c.pseudo_citations).join(", ")}`);
  if (c.unsupported_removal_claim === true) {
    out.badges.push({ text: 'claims a removal that the "no longer appears" lists do not support', cls: "warn" });
    const sentences = list(c.unsupported_removal_sentences);
    out.warnings.push(`A sentence claims a removal that the "no longer appears" lists of the retrieved context do not support${sentences.length ? `: ${sentences.join(" / ")}` : ""}`);
  }
}

/** The badges and visible warnings that `checks` supports. Never claims more than the checks do. Never throws. */
export function checksSummary(event: unknown): ChecksSummary {
  const ev = dictOf(event);
  const cites = list(ev.citations);
  const hall = list(ev.hallucinated);
  const c = isDict(ev.checks) ? ev.checks : null;
  const out: ChecksSummary = { badges: [{ text: `${cites.length} citation${cites.length === 1 ? "" : "s"}`, cls: "" }], warnings: [], hint: CHECKS_HINT };
  const unmatched = c ? list(c.unmatched_numbers) : [];
  const echoed = c ? list(c.echoed_numbers) : [];
  citationBadges(cites, hall, c, out);
  numberBadges(c, unmatched, echoed, out);
  warningLines(c, unmatched, echoed, out);
  return out;
}

/** The whole footer of an answer: the usage and cost (or the cached note), the answer badge, the checks badges, the warnings, the hint. */
export function metaSummary(event: unknown, defaultModel?: unknown): ChecksSummary {
  const ev = dictOf(event);
  const head: Badge[] = [];
  if (ev.cached) {
    head.push({ text: `cached \u00b7 ${ev.source === "benchmark" ? "saved example answer from an earlier evaluation run" : "answered earlier"}`, cls: "" });
  } else {
    const usage = dictOf(ev.usage);
    head.push({ text: `${usage.prompt_tokens || "?"} in / ${usage.completion_tokens || "?"} out tokens${usage.estimated ? " (est.)" : ""}`, cls: "" });
    if (typeof ev.cost_usd === "number") head.push({ text: `\u2248 $${ev.cost_usd.toFixed(3)}`, cls: "" });
  }
  const badge = answerBadge(ev, defaultModel);
  if (badge) head.push(badge);
  if (ev.finish_reason === "length") head.push({ text: "answer truncated by the token budget", cls: "warn" });
  const checks = checksSummary(ev);
  return { badges: [...head, ...checks.badges], warnings: checks.warnings, hint: checks.hint };
}
