// R32, R34 (docs/specs/m5b-frontend.md): the workspace session, its documents, the upload queue and the job watchers, above the
// router so a route change neither loses them nor stops a multi-file drop. Two contexts come out of it: main's frozen
// WorkspaceContext (./useWorkspace) and this worker's WorkspaceUploadsContext (./useWorkspaceUploads).
//
// IDENTITY. The workspace `id` (null when closed). A call of `setWorkspace` that changes it bumps `generation` and
// `docEvidenceEpoch`, resets the as-of value, empties the documents and ABORTS the upload queue (its POST, its watcher, the files
// still waiting), so nothing of the old workspace is ever posted to the new one. A call with the same id (a restored session
// getting its `expires_at` filled in) replaces `workspace` and bumps nothing.
//
// STORAGE. The session is written to sessionStorage (lib/workspace.ts: exactly {id, token, expires_at}) by `setWorkspace`, in a
// handler or an async continuation, never in an effect. The load from sessionStorage is a GET only: it starts no watcher
// (GET /api/workspace lists no running jobs) and spends nothing. 404 closes the workspace; any other failure keeps it.
//
// REQUESTS THAT SPEND. Create and upload start from handlers (`create`, `upload`) only. The queue is made in an effect and
// detached in its cleanup; StrictMode runs that twice, which leaves one armed queue (the first is aborted), so an upload made
// after the remount still goes out and a teardown still aborts the live watcher.
//
// The provider is a composition of small hooks (useAsOf, useSession, useDocuments, useQueue, useHandlers) so each stays short.
import { useCallback, useEffect, useMemo, useRef, useState, type MutableRefObject, type ReactNode } from "react";
import { UploadQueue, type JobRow } from "@/lib/jobs";
import {
  MSG_NOT_FOUND,
  createWorkspace,
  deleteWorkspace,
  isWorkspaceId,
  isWorkspaceToken,
  loadWorkspace,
  readStoredWorkspace,
  restoreWorkspace,
  writeStoredWorkspace,
  type WsDocument,
} from "@/lib/workspace";
import { useTurnstile, type TurnstileContextValue } from "./useTurnstile";
import { WorkspaceContext, type WorkspaceContextValue, type WorkspaceSession } from "./useWorkspace";
import { WorkspaceUploadsContext, type WorkspaceUploadsValue } from "./useWorkspaceUploads";

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

/** An as-of instant that no document offers any more (a date is never dropped: the server's guard decides). */
function isOrphanInstant(asOf: string, docs: readonly WsDocument[]): boolean {
  if (asOf === "" || DATE_ONLY.test(asOf)) return false;
  return !docs.some((d) => d.versions.some((v) => v.created_at === asOf));
}

type QueueRef = MutableRefObject<UploadQueue | null>;
type GateRef = MutableRefObject<TurnstileContextValue>;

function useAsOf() {
  const [asOf, setAsOfState] = useState("");
  const asOfRef = useRef("");
  const setAsOf = useCallback((value: string) => {
    asOfRef.current = value;
    setAsOfState(value);
  }, []);
  return { asOf, asOfRef, setAsOf };
}

/** The session, its identity rules and the signals derived from them. */
function useSession(queueRef: QueueRef, setAsOf: (value: string) => void) {
  const [workspace, setSession] = useState<WorkspaceSession | null>(() => readStoredWorkspace());
  const [generation, setGeneration] = useState(0);
  const [docEvidenceEpoch, setDocEvidenceEpoch] = useState(0);
  const [documents, setDocuments] = useState<readonly WsDocument[]>([]);
  const [announcement, setAnnouncement] = useState("");
  const session = useRef(workspace);
  const identity = useRef<string | null>(workspace?.id ?? null);
  const readSeq = useRef(0);
  const invalidateDocEvidence = useCallback(() => setDocEvidenceEpoch((n) => n + 1), []);
  // reads are numbered: a read answers only while it is still the latest and the workspace has not changed
  const beginRead = useCallback(() => (readSeq.current += 1), []);
  const isLatestRead = useCallback((seq: number) => seq === readSeq.current, []);

  const setWorkspace = useCallback(
    (next: WorkspaceSession | null) => {
      session.current = next;
      setSession(next);
      writeStoredWorkspace(next);
      const nextId = next === null ? null : next.id;
      if (nextId === identity.current) return;
      identity.current = nextId;
      queueRef.current?.abortSession();
      readSeq.current += 1; // a read still in flight belongs to the old workspace
      setGeneration((n) => n + 1);
      setDocEvidenceEpoch((n) => n + 1);
      setAsOf("");
      setDocuments([]);
      setAnnouncement("");
    },
    [queueRef, setAsOf],
  );
  return {
    workspace,
    session,
    beginRead,
    isLatestRead,
    generation,
    docEvidenceEpoch,
    invalidateDocEvidence,
    setWorkspace,
    documents,
    setDocuments,
    announcement,
    setAnnouncement,
  };
}

type Session = ReturnType<typeof useSession>;

/** Reading the open workspace's documents: on load (a GET, aborted on teardown) and whenever a job or a handler asks. */
function useDocuments(s: Session, asOfRef: MutableRefObject<string>, setAsOf: (value: string) => void) {
  const { session, beginRead, isLatestRead, setWorkspace, setDocuments } = s;
  const applyDocuments = useCallback(
    (docs: readonly WsDocument[]) => {
      setDocuments(docs);
      if (isOrphanInstant(asOfRef.current, docs)) setAsOf("");
    },
    [setDocuments, asOfRef, setAsOf],
  );
  /** A 404 closes the workspace; another failure keeps the last known state. A newer read or a change of workspace wins. */
  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      const current = session.current;
      if (current === null) return;
      const seq = beginRead();
      const result = await loadWorkspace(current, signal);
      if (signal?.aborted === true || !isLatestRead(seq) || session.current?.id !== current.id) return;
      if (result.kind === "gone") setWorkspace(null);
      else if (result.kind === "ok") {
        applyDocuments(result.documents);
        if (result.expires_at !== null && current.expires_at !== result.expires_at) setWorkspace({ ...current, expires_at: result.expires_at });
      }
    },
    [session, beginRead, isLatestRead, setWorkspace, applyDocuments],
  );
  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);
  return { applyDocuments, refresh };
}

type Docs = ReturnType<typeof useDocuments>;

/** One queue per mount (StrictMode's mount, unmount, mount ends with one armed queue and the first aborted). Its callbacks run
 *  outside render: a terminal watch signals the drawer and reads the documents again. */
function useQueue(queueRef: QueueRef, gate: GateRef, s: Session, d: Docs) {
  const { invalidateDocEvidence, setAnnouncement } = s;
  const { refresh } = d;
  const [rows, setRows] = useState<readonly JobRow[]>([]);
  useEffect(() => {
    const created = new UploadQueue({
      token: {
        siteKeyKnown: () => gate.current.siteKeyState.kind !== "unknown",
        siteKeySet: () => gate.current.siteKeySet,
        consume: () => gate.current.consume(),
        waitForToken: (ms, signal) => gate.current.waitForToken(ms, signal),
      },
      onRows: setRows,
      onTerminal: () => {
        invalidateDocEvidence();
        void refresh();
      },
      onUnchanged: () => void refresh(),
      onAnnounce: setAnnouncement,
    });
    created.attach();
    queueRef.current = created;
    return () => {
      created.detach();
      if (queueRef.current === created) queueRef.current = null;
    };
  }, [queueRef, gate, refresh, invalidateDocEvidence, setAnnouncement]);
  return rows;
}

function useHandlers(queueRef: QueueRef, gate: GateRef, s: Session, d: Docs) {
  const { session, setWorkspace, setDocuments } = s;
  const { applyDocuments } = d;
  const create = useCallback(async (): Promise<string> => {
    const token = gate.current.consume();
    const result = await createWorkspace(token);
    if (token === null) gate.current.clear(); // reset after every use, as the legacy page does (index.html:1050)
    if (result.kind === "error") return result.detail;
    setWorkspace(result.session);
    setDocuments([]);
    return "";
  }, [gate, setWorkspace, setDocuments]);
  const restore = useCallback(
    async (rawId: string, rawToken: string): Promise<string> => {
      const id = rawId.trim();
      const token = rawToken.trim();
      if (id === "" || token === "") return "";
      if (!isWorkspaceId(id) || !isWorkspaceToken(token)) return MSG_NOT_FOUND;
      const result = await restoreWorkspace(id, token);
      if (result.kind === "notFound") return MSG_NOT_FOUND;
      setWorkspace(result.session);
      applyDocuments(result.documents);
      return "";
    },
    [applyDocuments, setWorkspace],
  );
  const remove = useCallback(async (): Promise<void> => {
    const current = session.current;
    if (current === null) return;
    await deleteWorkspace(current);
    setWorkspace(null);
  }, [session, setWorkspace]);
  const upload = useCallback(
    (files: readonly File[], documentId: string) => {
      const current = session.current;
      if (current !== null) queueRef.current?.enqueue(current, files, documentId);
    },
    [session, queueRef],
  );
  return useMemo(() => ({ create, restore, remove, upload }), [create, restore, remove, upload]);
}

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const turnstile = useTurnstile();
  const gate = useRef(turnstile);
  useEffect(() => {
    gate.current = turnstile;
  }, [turnstile]);
  const queueRef = useRef<UploadQueue | null>(null);
  const { asOf, asOfRef, setAsOf } = useAsOf();
  const s = useSession(queueRef, setAsOf);
  const d = useDocuments(s, asOfRef, setAsOf);
  const rows = useQueue(queueRef, gate, s, d);
  const handlers = useHandlers(queueRef, gate, s, d);

  const { workspace, generation, docEvidenceEpoch, invalidateDocEvidence, setWorkspace, documents, announcement } = s;
  const core = useMemo<WorkspaceContextValue>(
    () => ({
      workspace,
      asOf,
      setAsOf,
      setWorkspace,
      generation,
      docEvidenceEpoch,
      invalidateDocEvidence,
      headers: (): Record<string, string> => (workspace ? { "X-Workspace-Token": workspace.token } : {}),
    }),
    [workspace, asOf, setAsOf, setWorkspace, generation, docEvidenceEpoch, invalidateDocEvidence],
  );
  const uploads = useMemo<WorkspaceUploadsValue>(
    () => ({ documents, rows, announcement, ...handlers }),
    [documents, rows, announcement, handlers],
  );
  return (
    <WorkspaceContext.Provider value={core}>
      <WorkspaceUploadsContext.Provider value={uploads}>{children}</WorkspaceUploadsContext.Provider>
    </WorkspaceContext.Provider>
  );
}
