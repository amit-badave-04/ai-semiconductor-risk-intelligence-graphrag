// R28, S2, S4, S11 (docs/specs/m5b-frontend.md): the what-changed report of GET /api/workspace/{id}/changes as data (index.html:
// 491-530). The report describes an UPLOADED document, so it is untrusted: the parser keeps only the fields the view shows,
// takes every field of a mistyped value as empty, and keeps a passage's citation id only when it matches the generated grammar
// (classifyId), so a chip can never be built from anything else. Nothing here is ever a URL or markup; the view renders text nodes.
import { deck } from "@/copy/deck";
import type { WorkspaceSession } from "@/state/useWorkspace";
import { classifyId } from "./citations";
import { detailOf } from "./refusal";
import { isWorkspaceId, isWorkspaceToken } from "./workspace";

export interface ChangePassage {
  quote: string | null;
  /** A citation id that matched the generated grammar, or null. */
  chunkId: string | null;
}

export interface ChangeItem {
  label: string;
  passages: ChangePassage[];
}

export type ChangeReport =
  | { kind: "unavailable"; reason: string }
  | {
      kind: "compared";
      unchanged: number;
      added: ChangeItem[];
      removed: ChangeItem[];
      changed: ChangeItem[];
      /** Units the aligner touched but with no surviving passage: listed apart, never folded into `unchanged`. */
      minor: string[];
      /** Sections compared as whole sections, not sentence by sentence for negation changes. */
      skipped: number;
    };

const UNKNOWN_REASON = deck.changesReport.unknownReason;
const UNTITLED = "(untitled)";
export const MSG_CHANGES_NETWORK = deck.changesReport.network;
export const MSG_CHANGES_FAILED = deck.changesReport.failed;

type Row = Record<string, unknown>;

function isRow(value: unknown): value is Row {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function rows(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function count(value: unknown): number {
  const n = typeof value === "string" && value.trim() !== "" ? Number(value) : value;
  return typeof n === "number" && Number.isFinite(n) && n > 0 ? n : 0;
}

function labelOf(item: unknown): string {
  const row = isRow(item) ? item : {};
  return text(row.headline) || text(row.older_unit_id) || text(row.newer_unit_id) || UNTITLED;
}

function toPassage(value: unknown): ChangePassage {
  const row = isRow(value) ? value : {};
  const id = typeof row.chunk_id === "string" && classifyId(row.chunk_id) !== null ? row.chunk_id : null;
  return { quote: text(row.quote) || null, chunkId: id };
}

function toItem(value: unknown): ChangeItem {
  const row = isRow(value) ? value : {};
  const passages = rows(row.passages).map(toPassage).filter((p) => p.quote !== null || p.chunkId !== null);
  return { label: labelOf(value), passages };
}

/** The report of a 200 body. A body that is not a compared report is "unavailable" with its reason, as the legacy page says. */
export function parseChangeReport(body: unknown): ChangeReport {
  const report = isRow(body) ? body : {};
  if (!report.items_compared) {
    return { kind: "unavailable", reason: text(report.not_compared_reason) || UNKNOWN_REASON };
  }
  return {
    kind: "compared",
    unchanged: count(report.unchanged_count),
    added: rows(report.added).map(toItem),
    removed: rows(report.removed).map(toItem),
    changed: rows(report.changed).map(toItem),
    minor: rows(report.minor_rewordings).map(labelOf),
    skipped: count(report.negation_check_skipped),
  };
}

export type ChangesResult = { kind: "ok"; report: ChangeReport } | { kind: "error"; detail: string };

function isVersion(value: number): boolean {
  return Number.isInteger(value) && value > 0;
}

/** GET the change report of one document between two of its versions. No request at all for a malformed id, token, document or
 *  version. An abort rejects (the caller discards it); every other failure is a result. */
export async function fetchChanges(
  session: WorkspaceSession,
  documentId: string,
  older: number,
  newer: number,
  signal?: AbortSignal,
): Promise<ChangesResult> {
  if (!isWorkspaceId(session.id) || !isWorkspaceToken(session.token) || documentId === "" || !isVersion(older) || !isVersion(newer)) {
    return { kind: "error", detail: MSG_CHANGES_FAILED };
  }
  const url = `/api/workspace/${session.id}/changes?document_id=${encodeURIComponent(documentId)}&from=${older}&to=${newer}`;
  let response: Response;
  try {
    response = await fetch(url, { headers: { "X-Workspace-Token": session.token }, signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    return { kind: "error", detail: MSG_CHANGES_NETWORK };
  }
  if (!response.ok) return { kind: "error", detail: await detailOf(response) };
  try {
    return { kind: "ok", report: parseChangeReport(await response.json()) };
  } catch {
    return { kind: "error", detail: MSG_CHANGES_FAILED };
  }
}
