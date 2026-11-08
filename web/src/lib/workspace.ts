// R28, R32, S4, S8 (docs/specs/m5b-frontend.md): the workspace's storage shape, its three calls and the pure functions of the
// panel (ported from index.html:449-530 and :865-1110).
//   * The session is {id, token, expires_at} under "semigraph:workspace" in sessionStorage and nowhere else, so a workspace made
//     on /legacy is restored here in the same tab and the reverse. Every storage access is in try/catch; a value read back is
//     untrusted (the legacy page writes the same key) and used only when it matches the server's grammar.
//   * The workspace id goes into a path and the token into a header: both are checked against their grammar BEFORE any request,
//     so a malformed value makes no request (S4). The token is sent in the X-Workspace-Token header only (S8).
//   * Create, restore and delete are sent at once; none is retried (create spends a daily count).
import type { WorkspaceSession } from "@/state/useWorkspace";
import { detailOf } from "./refusal";

export const WS_SESSION_KEY = "semigraph:workspace";

// uploads.WORKSPACE_ID_RE: secrets.token_hex(16). A token is secrets.token_urlsafe(24); any printable ASCII without a space is
// accepted so a header value can never carry a line break or a control character (fetch throws on one).
const WORKSPACE_ID_RE = /^[0-9a-f]{32}$/;
const TOKEN_RE = /^[\x21-\x7e]{1,512}$/;

export const MSG_CREATE_NETWORK = "network error creating a workspace";
export const MSG_CREATE_FAILED = "could not create a workspace";
export const MSG_NOT_FOUND = "workspace not found";

export function isWorkspaceId(value: unknown): value is string {
  return typeof value === "string" && WORKSPACE_ID_RE.test(value);
}

export function isWorkspaceToken(value: unknown): value is string {
  return typeof value === "string" && TOKEN_RE.test(value);
}

type Row = Record<string, unknown>;

function isRow(value: unknown): value is Row {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function list<T>(value: readonly T[] | null | undefined): readonly T[] {
  return Array.isArray(value) ? value : [];
}

// ---------------------------------------------------------------- storage (R32)

function store(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null; // blocked or private: the workspace still works for this page load
  }
}

/** The stored session, or null when there is none or it is not shaped like one. */
export function readStoredWorkspace(): WorkspaceSession | null {
  try {
    const raw = store()?.getItem(WS_SESSION_KEY);
    if (!raw) return null;
    const value: unknown = JSON.parse(raw);
    if (!isRow(value) || !isWorkspaceId(value.id) || !isWorkspaceToken(value.token)) return null;
    if (value.expires_at !== undefined && value.expires_at !== null && typeof value.expires_at !== "string") return null;
    return { id: value.id, token: value.token, expires_at: str(value.expires_at) };
  } catch {
    return null;
  }
}

/** Persist the session (exactly the three fields) or, for null, forget it. Never throws. */
export function writeStoredWorkspace(session: WorkspaceSession | null): void {
  try {
    const target = store();
    if (target === null) return;
    if (session === null) target.removeItem(WS_SESSION_KEY);
    else target.setItem(WS_SESSION_KEY, JSON.stringify({ id: session.id, token: session.token, expires_at: session.expires_at }));
  } catch {
    // storage full or blocked
  }
}

// ---------------------------------------------------------------- documents

export interface WsVersion {
  version: number;
  created_at: string | null;
  is_current: boolean;
  status: string | null;
}

export interface WsDocument {
  document_id: string;
  title: string | null;
  latest_version: number | null;
  versions: WsVersion[];
}

function toVersion(row: unknown): WsVersion | null {
  if (!isRow(row) || typeof row.version !== "number" || !Number.isFinite(row.version)) return null;
  return { version: row.version, created_at: str(row.created_at), is_current: row.is_current === true, status: str(row.status) };
}

function toDocument(row: unknown): WsDocument | null {
  if (!isRow(row) || typeof row.document_id !== "string" || row.document_id === "") return null;
  const latest = row.latest_version;
  return {
    document_id: row.document_id,
    title: str(row.title),
    latest_version: typeof latest === "number" && Number.isFinite(latest) ? latest : null,
    versions: (Array.isArray(row.versions) ? row.versions : []).flatMap((v) => toVersion(v) ?? []),
  };
}

/** The documents of a GET /api/workspace body; what is not a document is skipped, a mistyped field is null. */
export function parseDocuments(body: unknown): WsDocument[] {
  if (!isRow(body) || !Array.isArray(body.documents)) return [];
  return body.documents.flatMap((d) => toDocument(d) ?? []);
}

// ---------------------------------------------------------------- calls

export type CreateResult = { kind: "ok"; session: WorkspaceSession } | { kind: "error"; detail: string };

/** POST /api/workspace with the bot-check token (or null) in the body: sent at once, never retried. */
export async function createWorkspace(turnstileToken: string | null): Promise<CreateResult> {
  let response: Response;
  try {
    response = await fetch("/api/workspace", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ turnstile_token: turnstileToken }),
    });
  } catch {
    return { kind: "error", detail: MSG_CREATE_NETWORK };
  }
  if (!response.ok) return { kind: "error", detail: await detailOf(response) };
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return { kind: "error", detail: MSG_CREATE_FAILED };
  }
  if (!isRow(body) || !isWorkspaceId(body.workspace_id) || !isWorkspaceToken(body.token)) {
    return { kind: "error", detail: MSG_CREATE_FAILED };
  }
  return { kind: "ok", session: { id: body.workspace_id, token: body.token, expires_at: str(body.expires_at) } };
}

export type LoadResult =
  | { kind: "ok"; expires_at: string | null; documents: WsDocument[] }
  | { kind: "gone" }
  | { kind: "error" };

/** GET /api/workspace/{id}: the documents. 404 means the workspace is gone; any other failure keeps the last known state. */
export async function loadWorkspace(session: WorkspaceSession, signal?: AbortSignal): Promise<LoadResult> {
  if (!isWorkspaceId(session.id) || !isWorkspaceToken(session.token)) return { kind: "error" };
  try {
    const response = await fetch(`/api/workspace/${session.id}`, { headers: { "X-Workspace-Token": session.token }, signal });
    if (response.status === 404) return { kind: "gone" };
    if (!response.ok) return { kind: "error" };
    const body: unknown = await response.json();
    return { kind: "ok", expires_at: isRow(body) ? str(body.expires_at) : null, documents: parseDocuments(body) };
  } catch {
    return { kind: "error" };
  }
}

export type RestoreResult =
  | { kind: "ok"; session: WorkspaceSession; documents: WsDocument[] }
  | { kind: "notFound" };

/** The Restore button: the server must confirm the id and token (a GET) before the workspace opens. */
export async function restoreWorkspace(id: string, token: string): Promise<RestoreResult> {
  const loaded = await loadWorkspace({ id, token, expires_at: null });
  if (loaded.kind !== "ok") return { kind: "notFound" };
  return { kind: "ok", session: { id, token, expires_at: loaded.expires_at }, documents: loaded.documents };
}

/** DELETE /api/workspace/{id}. The caller closes the workspace whatever this answers (as the legacy page does). */
export async function deleteWorkspace(session: WorkspaceSession): Promise<void> {
  if (!isWorkspaceId(session.id) || !isWorkspaceToken(session.token)) return;
  try {
    await fetch(`/api/workspace/${session.id}`, { method: "DELETE", headers: { "X-Workspace-Token": session.token } });
  } catch {
    // the workspace closes locally either way
  }
}

// ---------------------------------------------------------------- pure functions of the panel

export interface SelectOption {
  value: string;
  label: string;
}

/** The title field of an upload: the file name, cut to the server's own cap so a long name is never refused. */
export function uploadTitleFor(fileName: string | null | undefined, maxChars: number): string {
  return String(fileName ?? "").slice(0, maxChars);
}

/** The upload-target options: a new document, or a new version of an existing one (so v2 reaches the same document). */
export function uploadTargetOptions(
  docs: readonly Pick<WsDocument, "document_id" | "title">[] | null | undefined,
): SelectOption[] {
  return [
    { value: "", label: "Upload a new document" },
    ...list(docs).map((d) => ({ value: d.document_id, label: `New version of ${d.title || d.document_id}` })),
  ];
}

/** "ask as of vN" options of one document: each value is that version's own created_at instant. */
export function versionAskAsOfOptions(
  versions: readonly Pick<WsVersion, "version" | "created_at">[] | null | undefined,
): SelectOption[] {
  return [
    { value: "", label: "ask as of: current" },
    ...list(versions).map((v) => ({ value: v.created_at || "", label: `ask as of v${v.version}` })),
  ];
}

/** "v1 (superseded) -> v2 (current)" for one document's version rows, oldest first. */
export function versionTimeline(
  versions: readonly Pick<WsVersion, "version" | "is_current" | "status">[] | null | undefined,
): string {
  return list(versions)
    .map((v) => `v${v.version} (${v.is_current ? "current" : v.status || "superseded"})`)
    .join(" \u2192 ");
}

/** "stale" for a citation id the done event's workspace.stale_citations lists, else "". */
export function staleClass(id: string, staleIds: readonly string[] | null | undefined): string {
  return list(staleIds).includes(id) ? "stale" : "";
}
