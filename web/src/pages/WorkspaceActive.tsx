// R28, A3, A4 (docs/specs/m5b-frontend.md): the open workspace: its id and expiry, delete, the as-of date, the upload area, the
// documents and the what-changed view. The token is never shown. Chips in the what-changed view open the one drawer above the
// router (worker B's), which the drawer closes on a route change. A request for a report is a GET started by a click, and a newer
// click abandons the older request, so an old report never replaces a newer one.
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { fetchChanges } from "@/lib/changes";
import type { WsDocument } from "@/lib/workspace";
import { useWorkspace, type WorkspaceSession } from "@/state/useWorkspace";
import { useWorkspaceUploads } from "@/state/useWorkspaceUploads";
import { deck } from "@/copy/deck";
import { WorkspaceChanges, type ChangesView } from "./WorkspaceChanges";
import { WorkspaceDocuments } from "./WorkspaceDocuments";
import { WorkspaceUpload } from "./WorkspaceUpload";

const { AS_OF_HINT, AS_OF_TITLE, DELETE_BUTTON, EXPIRES_UNKNOWN, EXPIRES_WORD, WORKSPACE_WORD } = deck.workspace;

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

function useChanges(session: WorkspaceSession) {
  const [view, setView] = useState<ChangesView>({ kind: "idle" });
  const request = useRef<AbortController | null>(null);
  const shown = useRef(0);
  useEffect(() => () => request.current?.abort(), []);
  const show = useCallback(
    (doc: WsDocument, version: number) => {
      request.current?.abort();
      const older = doc.versions
        .map((v) => v.version)
        .filter((v) => v < version)
        .pop();
      if (older === undefined) {
        setView({ kind: "first", newer: version });
        return;
      }
      const controller = new AbortController();
      request.current = controller;
      setView({ kind: "loading", older, newer: version });
      fetchChanges(session, doc.document_id, older, version, controller.signal).then(
        (result) => {
          if (controller.signal.aborted) return;
          shown.current += 1;
          setView(
            result.kind === "ok"
              ? { kind: "report", older, newer: version, report: result.report, seq: shown.current }
              : { kind: "error", detail: result.detail },
          );
        },
        () => undefined, // an abort: a newer request owns the view
      );
    },
    [session],
  );
  return { view, show };
}

export function WorkspaceActive({ session }: { session: WorkspaceSession }) {
  const { asOf, setAsOf } = useWorkspace();
  const { documents, remove, announcement } = useWorkspaceUploads();
  const { view, show } = useChanges(session);
  const dateId = useId();
  return (
    <div>
      <div className="flex flex-wrap items-center gap-3">
        <span className="min-w-0 text-sm text-muted-foreground">
          {WORKSPACE_WORD} <code className="break-all">{session.id}</code>
          {` \u00b7 ${EXPIRES_WORD} ${session.expires_at ?? EXPIRES_UNKNOWN}`}
        </span>
        <Button type="button" variant="outline" onClick={() => void remove()}>
          {DELETE_BUTTON}
        </Button>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <label htmlFor={dateId} className="sr-only">
          {AS_OF_TITLE}
        </label>
        <input
          id={dateId}
          type="date"
          title={AS_OF_TITLE}
          value={DATE_ONLY.test(asOf) ? asOf : ""}
          onChange={(event) => setAsOf(event.target.value)}
          className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
        />
        <span className="text-sm text-muted-foreground">{AS_OF_HINT}</span>
      </div>
      <WorkspaceUpload documents={documents} />
      <WorkspaceDocuments documents={documents} asOf={asOf} setAsOf={setAsOf} onShowChanges={show} />
      <WorkspaceChanges view={view} />
      <output className="sr-only">
        {announcement}
      </output>
    </div>
  );
}
