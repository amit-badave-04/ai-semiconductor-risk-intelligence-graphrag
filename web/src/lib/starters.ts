// docs/specs/m5b-redesign.md RD5, decision D-R6: the six starter questions of the Ask page and the "Show all examples" list.
// GET /api/examples lists only the examples the service actually holds a saved answer for, so a starter is the curated id
// list intersected with that payload (a curated id the server does not list is dropped). Pure functions over the untrusted
// payload; every question is rendered as a text node by the caller.
import { FILERS } from "./pages.generated";

export type StarterJob = "company" | "number" | "rules" | "changes";

/** The curated starters, in the order the owner approved (D-R6). */
export const STARTERS: readonly { id: string; job: StarterJob }[] = [
  { id: "N2", job: "number" },
  { id: "D3", job: "company" },
  { id: "Q2", job: "company" },
  { id: "R1", job: "rules" },
  { id: "T7", job: "changes" },
  { id: "NG15", job: "number" },
];

/** Saved answers that contradict a detail of their own source: never offered as a starter (RD5). */
export const NEVER_STARTERS: readonly string[] = ["D1", "X4"];

/** The order the starter groups are shown in: understand a company, check a number, track export rules, see what changed. */
const JOB_ORDER: readonly StarterJob[] = ["company", "number", "rules", "changes"];

export const OTHER_COMPANY = "Other";

export interface Example {
  id: string;
  question: string;
}

export interface StarterGroup {
  job: StarterJob;
  examples: Example[];
}

export interface CompanyGroup {
  /** A covered company's name, or OTHER_COMPANY. */
  company: string;
  examples: Example[];
}

function examplesOf(payload: unknown): Example[] {
  const list = typeof payload === "object" && payload !== null ? (payload as { examples?: unknown }).examples : undefined;
  if (!Array.isArray(list)) return [];
  return list.flatMap((item) => {
    if (typeof item !== "object" || item === null) return [];
    const { id, question } = item as { id?: unknown; question?: unknown };
    return typeof id === "string" && typeof question === "string" ? [{ id, question }] : [];
  });
}

const escape = (text: string): string => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const NAME_PATTERNS = FILERS.map((f) => ({ name: f.name, pattern: new RegExp(`\\b${escape(f.name)}\\b`) }));

/** The covered company a question names first, or OTHER_COMPANY. */
function companyOf(question: string): string {
  let best: { name: string; at: number } | null = null;
  for (const { name, pattern } of NAME_PATTERNS) {
    const at = question.search(pattern);
    if (at >= 0 && (best === null || at < best.at)) best = { name, at };
  }
  return best?.name ?? OTHER_COMPANY;
}

/** The starters grouped by job, and every other example grouped by company (covered companies in FILERS order, Other last). */
export function splitExamples(payload: unknown): { groups: StarterGroup[]; byCompany: CompanyGroup[] } {
  const examples = examplesOf(payload);
  const byId = new Map(examples.map((e) => [e.id, e]));
  const starterIds = new Set<string>();
  const groups = JOB_ORDER.flatMap((job) => {
    const picked = STARTERS.filter((s) => s.job === job && !NEVER_STARTERS.includes(s.id)).flatMap((s) => {
      const found = byId.get(s.id);
      return found === undefined ? [] : [found];
    });
    picked.forEach((e) => starterIds.add(e.id));
    return picked.length === 0 ? [] : [{ job, examples: picked }];
  });

  const companies = new Map<string, Example[]>();
  for (const e of examples) {
    if (starterIds.has(e.id)) continue;
    const company = companyOf(e.question);
    companies.set(company, [...(companies.get(company) ?? []), e]);
  }
  const order = [...FILERS.map((f) => f.name as string), OTHER_COMPANY];
  const byCompany = order.flatMap((company) => {
    const list = companies.get(company);
    return list === undefined ? [] : [{ company, examples: list }];
  });
  return { groups, byCompany };
}
