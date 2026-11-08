// R28, R33(2), R34 (docs/specs/m5b-frontend.md): the upload queue of the workspace page. Framework-free so it can be tested without
// React; state/WorkspaceProvider.tsx owns one and feeds it.
//   * ONE FILE AT A TIME, each awaiting its job's end: the server holds a single upload slot until the job lands, so posting the
//     next file earlier would trade a stale token for a 429. A file that fails (refusal, network, no token, a lost watch) never
//     stops the queue: its own outcome stays in its own row and the next file gets its turn, as on the legacy page.
//   * AT MOST ONE WATCHER PER JOB: the queue awaits `watch` (lib/jobStream.ts, which itself keeps one open GET per watch) before
//     it takes the next file, and a watcher starts only from `enqueue`, which the upload handler calls, never from an effect.
//     Neither restore path (the button, the load from sessionStorage) starts one: GET /api/workspace lists no running jobs.
//   * TOKEN (R33(2)): with a site key set the queue uses the token it holds or waits up to 15 s for one; with none it fails the
//     file with the old row text and sends nothing. It never sends an empty X-Turnstile-Token. With no site key it sends no
//     such header at all (the server then decides, as the legacy page's empty header did).
//   * BOUND TO ITS SESSION: `enqueue` takes the session the files belong to. `abortSession` (the workspace's identity changed)
//     aborts the POST and the watcher in flight, drops the files still waiting and the rows, and re-arms the queue; a stale
//     pass never writes a row or posts a file into the next workspace. `detach` (the provider tears down) aborts the same way
//     and leaves the queue disarmed until `attach`: StrictMode's mount, unmount, mount therefore ends armed.
//   * `onTerminal` is called ONCE per watch, from its result: a terminal job, a job that is gone, a watch that gave up and a
//     watch that threw (the job may have finished unseen, and a new version changes the status of an older version's chunks).
//     Never for an abort, never per event. `jobStream.watchJob` rethrows what `onJob` throws (the page's own failure); that is
//     caught here and shown as a lost connection, the nearest legacy text (there is none for it, nor for a buffer overflow).
import type { WorkspaceSession } from "@/state/useWorkspace";
import type { JobEvent } from "./events";
import { watchJob, type WatchOptions, type WatchResult } from "./jobStream";
import { detailOf } from "./refusal";
import { uploadTitleFor } from "./workspace";

/** How long an upload waits for a bot-check token (index.html:591). */
export const TOKEN_WAIT_MS = 15000;
/** The server's own title cap (MAX_TITLE_CHARS), so a long file name is never refused. */
export const MAX_UPLOAD_TITLE_CHARS = 120;

export const BOT_TIMEOUT_TEXT = "bot check did not complete in time \u2014 try again";
export const UPLOADING_TEXT = "uploading\u2026";
export const NETWORK_TEXT = "network error";
export const QUEUED_TEXT = "queued";
export const CANNOT_WATCH_TEXT = "could not watch this upload";
export const CONNECTION_LOST_TEXT = "connection lost while watching this upload \u2014 check its status later";

const JOB_STATE_TEXT: Readonly<Record<string, string>> = {
  received: "queued",
  validating: "checking",
  parsing: "reading the document",
  chunking: "splitting into sections",
  embedding: "embedding",
  comparing: "comparing with the previous version",
  indexing: "saving",
  ready: "ready",
  failed: "failed",
};

function isRow(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Human text for one job-progress event (index.html:471). Tolerates a missing or unknown job. */
export function jobStateText(job: unknown): string {
  const j = isRow(job) ? job : {};
  const state = typeof j.state === "string" ? j.state : "";
  const base = JOB_STATE_TEXT[state] ?? (state || "unknown");
  if (state === "embedding" && isRow(j.progress)) {
    const { done, total, eta_s: eta } = j.progress;
    const count = typeof done === "number" && typeof total === "number" ? ` ${done}/${total}` : "";
    const left = typeof eta === "number" && eta > 0 ? ` (~${Math.ceil(eta)}s left)` : "";
    return `${base}${count}${left}`;
  }
  if (state === "failed") {
    const error = isRow(j.error) ? j.error : {};
    const why = (typeof error.message === "string" && error.message) || (typeof error.code === "string" && error.code) || "unknown error";
    return `failed: ${why}`;
  }
  return base;
}

export interface JobRow {
  /** Unique per file, so two files with the same name never share a row. */
  key: number;
  name: string;
  text: string;
  tone: "" | "ok" | "err";
  /** The file's outcome is final (ready, failed, refused, unchanged...). */
  terminal: boolean;
}

/** The part of the Turnstile gate the queue uses (state/useTurnstile.ts). */
export interface TokenSource {
  /** False while /api/config has not given a settled answer (loading, or failed): unknown is never taken for "no key" (R8). */
  siteKeyKnown: () => boolean;
  siteKeySet: () => boolean;
  /** Use the held token up (the widget resets), or null. */
  consume: () => string | null;
  /** A token already consumed, or null on timeout, abort or no key. */
  waitForToken: (ms: number, signal?: AbortSignal) => Promise<string | null>;
}

export interface QueueDeps {
  token: TokenSource;
  onRows: (rows: readonly JobRow[]) => void;
  /** A watch ended (terminal, gone, gave up or threw): drop cached doc evidence and reload the documents. */
  onTerminal: () => void;
  /** An identical re-upload was recognised: reload the documents. */
  onUnchanged: () => void;
  /** A file reached its final state: one line for the polite status region (A3). */
  onAnnounce: (text: string) => void;
  watch?: (workspaceId: string, jobId: string, token: string, options: WatchOptions) => Promise<WatchResult>;
}

interface Pending {
  session: WorkspaceSession;
  file: File;
  documentId: string;
}

/** The pass a piece of work belongs to: an abort bumps the epoch, and work of an older epoch stops writing. */
interface Pass {
  epoch: number;
  signal: AbortSignal;
}

export class UploadQueue {
  private controller: AbortController | null = null;
  private epoch = 0;
  private running = false;
  private pending: Pending[] = [];
  private rows: JobRow[] = [];
  private nextKey = 0;

  private readonly deps: QueueDeps;

  constructor(deps: QueueDeps) {
    this.deps = deps;
  }

  /** True while a file is being uploaded or watched, or files are waiting. */
  get busy(): boolean {
    return this.running || this.pending.length > 0;
  }

  /** Arm the queue (a provider mount). */
  attach(): void {
    if (this.controller === null) this.controller = new AbortController();
  }

  /** Abort everything and disarm (a provider teardown). */
  detach(): void {
    this.abortAll();
    this.controller = null;
  }

  /** The workspace's identity changed: abort everything of the old one and stay armed for the new one. */
  abortSession(): void {
    this.abortAll();
    this.controller = new AbortController();
    this.emit();
  }

  /** Queue files of `session`. Called from the upload handler only. */
  enqueue(session: WorkspaceSession, files: readonly File[], documentId: string): void {
    if (this.controller === null || files.length === 0) return;
    for (const file of files) this.pending.push({ session, file, documentId });
    if (!this.running) void this.drain({ epoch: this.epoch, signal: this.controller.signal });
  }

  private abortAll(): void {
    this.controller?.abort();
    this.epoch += 1;
    this.running = false;
    this.pending = [];
    this.rows = [];
  }

  private emit(): void {
    this.deps.onRows([...this.rows]);
  }

  private live(pass: Pass): boolean {
    return pass.epoch === this.epoch && !pass.signal.aborted;
  }

  private setRow(pass: Pass, key: number, patch: Partial<Omit<JobRow, "key">>): void {
    if (!this.live(pass)) return;
    this.rows = this.rows.map((row) => (row.key === key ? { ...row, ...patch } : row));
    this.emit();
    const row = this.rows.find((r) => r.key === key);
    if (row?.terminal === true && patch.terminal === true) this.deps.onAnnounce(`${row.name}: ${row.text}`);
  }

  private async drain(pass: Pass): Promise<void> {
    this.running = true;
    try {
      while (this.live(pass)) {
        const next = this.pending.shift();
        if (next === undefined) break;
        await this.uploadOne(pass, next);
      }
    } finally {
      if (pass.epoch === this.epoch) this.running = false;
    }
  }

  private async tokenFor(pass: Pass): Promise<{ ok: true; token: string | null } | { ok: false }> {
    if (!this.deps.token.siteKeyKnown()) return { ok: false }; // R8: it is not known whether a check is due, so nothing is sent
    if (!this.deps.token.siteKeySet()) return { ok: true, token: null };
    const token = this.deps.token.consume() ?? (await this.deps.token.waitForToken(TOKEN_WAIT_MS, pass.signal));
    return token === null ? { ok: false } : { ok: true, token };
  }

  private async uploadOne(pass: Pass, item: Pending): Promise<void> {
    const key = (this.nextKey += 1);
    this.rows = [...this.rows, { key, name: item.file.name, text: UPLOADING_TEXT, tone: "", terminal: false }];
    this.emit();
    const bot = await this.tokenFor(pass);
    if (!bot.ok) {
      this.setRow(pass, key, { text: BOT_TIMEOUT_TEXT, tone: "err", terminal: true });
      return;
    }
    const body = new FormData();
    body.append("file", item.file);
    body.append("title", uploadTitleFor(item.file.name, MAX_UPLOAD_TITLE_CHARS));
    if (item.documentId !== "") body.append("document_id", item.documentId);
    const headers: Record<string, string> = { "X-Workspace-Token": item.session.token };
    if (bot.token !== null) headers["X-Turnstile-Token"] = bot.token;
    let response: Response;
    try {
      response = await fetch(`/api/workspace/${item.session.id}/documents`, { method: "POST", headers, body, signal: pass.signal });
    } catch {
      this.setRow(pass, key, { text: NETWORK_TEXT, tone: "err", terminal: true });
      return;
    }
    await this.afterUpload(pass, key, item, response);
  }

  private async afterUpload(pass: Pass, key: number, item: Pending, response: Response): Promise<void> {
    if (!response.ok) {
      const detail = await detailOf(response);
      this.setRow(pass, key, { text: detail, tone: "err", terminal: true });
      return;
    }
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      // an accepted upload whose body cannot be read has no job to watch
    }
    const fields = isRow(body) ? body : {};
    if (fields.unchanged === true) {
      this.setRow(pass, key, { text: `unchanged (still v${String(fields.version)})`, tone: "ok", terminal: true });
      if (this.live(pass)) this.deps.onUnchanged();
      return;
    }
    if (typeof fields.job_id !== "string") {
      this.setRow(pass, key, { text: CANNOT_WATCH_TEXT, tone: "err", terminal: true });
      if (this.live(pass)) this.deps.onTerminal();
      return;
    }
    this.setRow(pass, key, { text: QUEUED_TEXT });
    await this.watchOne(pass, key, item, fields.job_id);
  }

  private async watchOne(pass: Pass, key: number, item: Pending, jobId: string): Promise<void> {
    const onJob = (job: JobEvent): void => {
      const final = job.state === "ready" || job.state === "failed";
      this.setRow(pass, key, { text: jobStateText(job), tone: job.state === "failed" ? "err" : job.state === "ready" ? "ok" : "", terminal: final });
    };
    const watch = this.deps.watch ?? watchJob;
    let result: WatchResult | { kind: "threw" };
    try {
      result = await watch(item.session.id, jobId, item.session.token, { onJob, signal: pass.signal });
    } catch {
      result = { kind: "threw" };
    }
    if (result.kind === "aborted" || !this.live(pass)) return;
    if (result.kind === "gone" || (result.kind === "gave_up" && result.reason === "status")) {
      this.setRow(pass, key, { text: CANNOT_WATCH_TEXT, tone: "err", terminal: true });
    } else if (result.kind === "gave_up" || result.kind === "threw") {
      this.setRow(pass, key, { text: CONNECTION_LOST_TEXT, tone: "err", terminal: true });
    }
    this.deps.onTerminal();
  }
}
