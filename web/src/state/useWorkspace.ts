// FROZEN CONTRACT (I3 main-first, 2026-11-08): the workspace session, as worker C2's WorkspaceProvider (state/WorkspaceProvider.tsx)
// will provide it. The Ask page (worker B) reads it to decide whether an ask is a workspace ask and to show the as-of date; the
// uploads, the job watchers and the what-changed view (worker C2) own everything else. The types, the hook and the behaviour
// promised below do not change without main.
//
// R32/S8: the session is `{id, token, expires_at}` under the key `semigraph:workspace` in sessionStorage and nowhere else (no
// localStorage, no cookie, no URL). The token goes ONLY into the X-Workspace-Token header (`headers()`): never into a URL, a
// title, DOM text, the console or an error text.
import { createContext, useContext } from "react";

export interface WorkspaceSession {
  /** 32 lower-case hex characters. */
  id: string;
  /** Secret. Header only. */
  token: string;
  /** ISO timestamp from the create call; null for a restored session whose expiry is not known yet. */
  expires_at: string | null;
}

export interface WorkspaceContextValue {
  /** The open workspace, or null. */
  workspace: WorkspaceSession | null;
  /**
   * The as-of value for workspace asks: a date (YYYY-MM-DD) or an instant (an ISO timestamp with a UTC offset, such as one
   * version's `created_at` for "ask as of vN"), SENT VERBATIM as `as_of` and never validated or reformatted by the client (the
   * server's guard decides, MSG_AS_OF). "" means current: send no `as_of`. Used only when a workspace is open. Reset to "" by
   * every change of the workspace's identity (the legacy `wsAskAsOfInstant = null`), so a new workspace never inherits the old
   * one's instant; a call that keeps the identity leaves it alone.
   */
  asOf: string;
  setAsOf: (value: string) => void;
  /**
   * Open (a session) or close (null) the workspace: state, and sessionStorage in try/catch (R32). The IDENTITY is the workspace
   * `id` (null when closed). A call that changes it (create, restore, delete, a different id) bumps `generation` and
   * `docEvidenceEpoch` and resets `asOf`; a call with the same id (a restored session getting its `expires_at` filled in) only
   * replaces `workspace` and bumps nothing.
   */
  setWorkspace: (next: WorkspaceSession | null) => void;
  /**
   * Starts at 0 and increases by exactly 1 per change of the workspace identity, never otherwise (not once per `setWorkspace`
   * call). State keyed to one workspace compares it.
   */
  generation: number;
  /**
   * The doc-evidence signal (the legacy `docEvidenceGen`). Starts at 0; +1 on every identity change AND on every call of
   * `invalidateDocEvidence`. The evidence drawer (below this provider in App.tsx) reads it: a new value drops its cached `doc:`
   * evidence and refuses to cache a `doc:` fetch that began before it. The drawer is the ONE owner of that clearing.
   */
  docEvidenceEpoch: number;
  /**
   * Bump `docEvidenceEpoch`. The upload queue and the job watchers (which live in this provider, so they cannot reach the drawer)
   * call it on EVERY terminal job state (ready, failed, gone) and when a watcher gives up (connection lost), whichever route the
   * visitor is on, because a new version changes the status of an older version's chunks (index.html:1003-1005, 1026).
   */
  invalidateDocEvidence: () => void;
  /** `{ "X-Workspace-Token": token }` when a workspace is open, else `{}`. A fresh object on every call. */
  headers: () => Record<string, string>;
}

const INERT: WorkspaceContextValue = {
  workspace: null,
  asOf: "",
  setAsOf: () => {},
  setWorkspace: () => {},
  generation: 0,
  docEvidenceEpoch: 0,
  invalidateDocEvidence: () => {},
  headers: () => ({}),
};

export const WorkspaceContext = createContext<WorkspaceContextValue>(INERT);

/** The workspace session. Outside a provider: no workspace, setters do nothing (keeps component tests simple). */
export function useWorkspace(): WorkspaceContextValue {
  return useContext(WorkspaceContext);
}
