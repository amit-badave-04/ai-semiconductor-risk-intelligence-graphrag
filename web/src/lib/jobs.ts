// R28, R33(2), R34 (docs/specs/m5b-frontend.md): the upload queue of the workspace page. Framework-free so it can be tested without
// React; state/WorkspaceProvider.tsx owns one and feeds it.
//   * ONE FILE AT A TIME, each awaiting its job's end: the server holds a single upload slot until the job lands, so posting the
//     next file earlier would trade a stale token for a 429. A file that fails (refusal, network, no token, a lost watch) never
//     stops the queue: its own outcome stays in its own row and the next file gets its turn, as on the legacy page.
//   * AT MOST ONE WATCHER PER JOB: the queue awaits `watch` (lib/jobStream.ts, which itself keeps one open GET per watch) before
//     it takes the next file, and a watcher starts only from `enqueue`, which the upload handler calls, never from an effect.
//     Neither restore path (the button, the load from sessionStorage) starts one: GET /api/workspace lists no running jobs.
//   * TOKEN (R33(2)): with a site key set the queue uses the token it holds or waits up to 15 s for one; with none it fails the
//     file with the old row text and sends nothing. A site key state that is still unknown (R8) is waited for up to 15 s first:
//     a /api/config that FAILED reads CONFIG_FAILED_TEXT, one still loading after the wait reads the old row text. It never sends an empty X-Turnstile-Token. With no site key it sends no
//     such header at all (the server then decides, as the legacy page's empty header did).
//   * BOUND TO ITS SESSION: `enqueue` takes the session the files belong to. `abortSession` (the workspace's identity changed)
//     aborts the POST and the watcher in flight, drops the files still waiting and the rows, and re-arms the queue; a stale
//     pass never writes a row or posts a file into the next workspace. `detach` (the provider tears down) aborts the same way
//     and leaves the queue disarmed until `attach`: StrictMode's mount, unmount, mount therefore ends armed.
//   * `onTerminal` is called ONCE per watch, from its result: a terminal job, a job that is gone, a watch that gave up and a
//     watch that threw (the job may have finished unseen, and a new version changes the status of an older version's chunks).
//     Never for an abort, never per event. `jobStream.watchJob` rethrows what `onJob` throws (the page's own failure); that is
//     caught here and shown as a lost connection, the nearest legacy text (there is none for it, nor for a buffer overflow).
import { deck } from "@/copy/deck";
import type { WorkspaceSession } from "@/state/useWorkspace";
import type { JobEvent } from "./events";
import { foldJob, type JobView } from "./jobView";
import { JOB_ID_RE, WORKSPACE_ID_RE, watchJob, type WatchOptions, type WatchResult } from "./jobStream";
import { detailOf } from "./refusal";
import { uploadTitleFor } from "./workspace";

/** How long an upload waits for a bot-check token (index.html:591). */
export const TOKEN_WAIT_MS = 15000;
/** The server's own title cap (MAX_TITLE_CHARS), so a long file name is never refused. */
export const MAX_UPLOAD_TITLE_CHARS = 120;

export const BOT_TIMEOUT_TEXT = deck.jobs.botTimeout;
/** Owner 2026-11-08: /api/config FAILED, so it is unknown whether a bot check is due (R8): reloading is the way out. */
export const CONFIG_FAILED_TEXT = deck.jobs.configFailed;
export const UPLOADING_TEXT = "uploading\u2026";
export const NETWORK_TEXT = deck.jobs.network;
export const QUEUED_TEXT = "queued";
export const CANNOT_WATCH_TEXT = deck.jobs.cannotWatch;
export const CONNECTION_LOST_TEXT = deck.jobs.connectionLost;

const JOB_STATE_TEXT: Readonly<Record<string, string>> = {
  received: "queued",
  validating: "checking",
  parsing: deck.jobs.phase.parsing,
  chunking: deck.jobs.phase.chunking,
  // the row stays at the keyword-ready line while search by meaning is prepared: the card shows the progress (council wording)
  embedding: deck.jobs.keywordReady,
  comparing: deck.jobs.phase.comparing,
  indexing: "saving",
  keyword_ready: deck.jobs.keywordReady,
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
  if (state === "failed") {
    const error = isRow(j.error) ? j.error : {};
    const why = (typeof error.message === "string" && error.message) || (typeof error.code === "string" && error.code) || deck.jobs.unknownError;
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
  /** The job being watched, once the server accepted the file (cancel needs it). */
  jobId: string | null;
  /** What the upload card shows (preflight, progress, outcome), folded from the job's events; null until one arrived. */
  view: JobView | null;
}

/** The part of the Turnstile gate the queue uses (state/useTurnstile.ts). */
export interface TokenSource {
  /** False while /api/config has not given a settled answer (loading, or failed): unknown is never taken for "no key" (R8). */
  siteKeyKnown: () => boolean;
  /** True once /api/config has answered badly (two attempts); false while it is still loading or when it answered. */
  siteKeyFailed: () => boolean;
  /** Wait up to `ms` for /api/config to settle: true when the site key state is known, false on timeout, abort or failure. */
  waitForSiteKey: (ms: number, signal?: AbortSignal) => Promise<boolean>;
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
  /** The session and job of each row that has one, for cancel. */
  private sessions = new Map<number, { session: WorkspaceSession; jobId: string }>();

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

  /** Ask the server to stop preparing search by meaning for one row's job (the document stays, keyword-searchable). The job still ends
   *  with `ready`, which the open watcher shows; a refusal or a lost connection returns false. */
  async cancel(key: number): Promise<boolean> {
    const known = this.sessions.get(key);
    if (known === undefined || this.controller === null) return false;
    if (!WORKSPACE_ID_RE.test(known.session.id) || !JOB_ID_RE.test(known.jobId)) return false;
    try {
      const res = await fetch(`/api/workspace/${known.session.id}/jobs/${known.jobId}/cancel`, {
        method: "POST",
        headers: { "X-Workspace-Token": known.session.token },
        signal: this.controller.signal,
      });
      return res.ok;
    } catch {
      // idempotent on the server; the watcher shows the job's real end. False lets the card give the stop button back.
      return false;
    }
  }

  private abortAll(): void {
    this.controller?.abort();
    this.epoch += 1;
    this.running = false;
    this.pending = [];
    this.rows = [];
    this.sessions.clear();
  }

  private emit(): void {
    this.deps.onRows([...this.rows]);
  }

  private live(pass: Pass): boolean {
    return pass.epoch === this.epoch && !pass.signal.aborted;
  }

  private setRow(pass: Pass, key: number, patch: Partial<Omit<JobRow, "key">>): void {
    if (!this.live(pass)) return;
    const cardShown = this.rows.find((r) => r.key === key)?.view?.keywordReady === true;
    this.rows = this.rows.map((row) => (row.key === key ? { ...row, ...patch } : row));
    this.emit();
    const row = this.rows.find((r) => r.key === key);
    // a card already on screen speaks its own end line in its live region; the global status line would say it twice
    if (row?.terminal === true && patch.terminal === true && !(cardShown && row.view?.outcome != null)) this.deps.onAnnounce(`${row.name}: ${row.text}`);
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

  private async tokenFor(pass: Pass): Promise<{ ok: true; token: string | null } | { ok: false; text: string }> {
    const source = this.deps.token;
    // R8: while it is not known whether a check is due nothing is sent. A config that is still loading is waited for (within
    // the same 15 s as a token: one budget for both waits); one that failed says so.
    let tokenWaitMs = TOKEN_WAIT_MS;
    if (!source.siteKeyKnown() && !source.siteKeyFailed()) {
      const deadline = Date.now() + TOKEN_WAIT_MS;
      await source.waitForSiteKey(TOKEN_WAIT_MS, pass.signal);
      tokenWaitMs = Math.max(0, deadline - Date.now());
    }
    if (!source.siteKeyKnown()) return { ok: false, text: source.siteKeyFailed() ? CONFIG_FAILED_TEXT : BOT_TIMEOUT_TEXT };
    if (!source.siteKeySet()) return { ok: true, token: null };
    const token = source.consume() ?? (await source.waitForToken(tokenWaitMs, pass.signal));
    return token === null ? { ok: false, text: BOT_TIMEOUT_TEXT } : { ok: true, token };
  }

  private async uploadOne(pass: Pass, item: Pending): Promise<void> {
    const key = (this.nextKey += 1);
    this.rows = [...this.rows, { key, name: item.file.name, text: UPLOADING_TEXT, tone: "", terminal: false, jobId: null, view: null }];
    this.emit();
    const bot = await this.tokenFor(pass);
    if (!bot.ok) {
      this.setRow(pass, key, { text: bot.text, tone: "err", terminal: true });
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
      this.setRow(pass, key, { text: deck.jobs.unchanged(String(fields.version)), tone: "ok", terminal: true });
      if (this.live(pass)) this.deps.onUnchanged();
      return;
    }
    if (typeof fields.job_id !== "string") {
      this.setRow(pass, key, { text: CANNOT_WATCH_TEXT, tone: "err", terminal: true });
      if (this.live(pass)) this.deps.onTerminal();
      return;
    }
    this.setRow(pass, key, { text: QUEUED_TEXT, jobId: fields.job_id });
    this.sessions.set(key, { session: item.session, jobId: fields.job_id });
    await this.watchOne(pass, key, item, fields.job_id);
  }

  private async watchOne(pass: Pass, key: number, item: Pending, jobId: string): Promise<void> {
    const onJob = (job: JobEvent): void => {
      const final = job.state === "ready" || job.state === "failed";
      const current = this.rows.find((r) => r.key === key);
      const view = job.state === "failed" ? (current?.view ?? null) : foldJob(current?.view ?? null, job);
      this.setRow(pass, key, { text: jobStateText(job), tone: job.state === "failed" ? "err" : job.state === "ready" ? "ok" : "", terminal: final, view });
      if (job.state === "keyword_ready" && current?.view?.keywordReady !== true && this.live(pass)) {
        this.deps.onAnnounce(`${item.file.name}: ${deck.jobs.card.keywordReady}`);
        this.deps.onUnchanged(); // the version is stored and searchable now: read the documents again
      }
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
