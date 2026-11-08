// Worker C2's second context (I3): everything the workspace page needs that the frozen WorkspaceContextValue (./useWorkspace) does
// not carry: the documents, the upload queue's rows, and the four handlers. It is provided by WorkspaceProvider (state that must
// outlive a route change, R34) and read by the page; main's contract is untouched. The hook is inert outside a provider.
//
// Handlers, not effects, start every request that spends something: `create` (a workspace), `upload` (an upload and its watcher).
// Their texts are the legacy page's (index.html:1037-1110), and a refusal's text is the server's own `detail`.
import { createContext, useContext } from "react";
import type { JobRow } from "@/lib/jobs";
import type { WsDocument } from "@/lib/workspace";

export interface WorkspaceUploadsValue {
  /** The open workspace's documents, as last read from the server ([] when there is none or none was read yet). */
  documents: readonly WsDocument[];
  /** One row per file of this visit's uploads (never persisted: a reload starts with none). */
  rows: readonly JobRow[];
  /** The latest terminal upload state as one line, for the page's polite status region (A3); "" until there is one. */
  announcement: string;
  /** Create a workspace, with the bot-check token it holds (or none), at once. Resolves with the text to show, "" on success. */
  create: () => Promise<string>;
  /** Open an existing workspace by id and token after the server confirms them. Resolves with the text to show, "" on success
   *  (and "" with no request when either field is empty). */
  restore: (id: string, token: string) => Promise<string>;
  /** Delete the workspace; it closes locally whatever the server answers. */
  remove: () => Promise<void>;
  /** Queue files for upload into the open workspace, one at a time. Called from the upload handler only. */
  upload: (files: readonly File[], documentId: string) => void;
}

const INERT: WorkspaceUploadsValue = {
  documents: [],
  rows: [],
  announcement: "",
  create: () => Promise.resolve(""),
  restore: () => Promise.resolve(""),
  remove: () => Promise.resolve(),
  upload: () => {},
};

export const WorkspaceUploadsContext = createContext<WorkspaceUploadsValue>(INERT);

export function useWorkspaceUploads(): WorkspaceUploadsValue {
  return useContext(WorkspaceUploadsContext);
}
