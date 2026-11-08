// R28 (docs/specs/m5b-frontend.md): the workspace page. Hidden until GET /api/stats says uploads are enabled (index.html:633): with
// them off, or while that is unknown, the page is its heading alone and makes no other request. Create, restore and delete are
// handlers over the provider (state/WorkspaceProvider.tsx), which owns the session, the documents and the upload queue, so a route
// change loses none of it. The restore token is read from an UNCONTROLLED password field and cleared after use, so it never sits in
// a `value` attribute or in React state (S8).
import { useEffect, useId, useRef, useState } from "react";
import { PageHeading } from "@/components/Layout";
import { Button } from "@/components/ui/button";
import { getStats, refreshStats, useStats } from "@/lib/statsStore";
import { useWorkspace } from "@/state/useWorkspace";
import { useWorkspaceUploads } from "@/state/useWorkspaceUploads";
import {
  CREATE_BUTTON,
  CREATING,
  HAVE_ONE,
  ID_LABEL,
  INTRO,
  PANEL_TITLE,
  RESTORE_BUTTON,
  RESTORING,
  TOKEN_LABEL,
} from "./Workspace.copy";
import { WorkspaceActive } from "./WorkspaceActive";

const FIELD = "h-9 w-[150px] rounded-md border border-input bg-transparent px-2.5 text-sm";

/** One action at a time (a double click sends one request), with the status text it shows: the working text, then the result. */
function useRun() {
  const [status, setStatus] = useState("");
  const busy = useRef(false);
  const run = async (working: string, action: () => Promise<string>): Promise<boolean> => {
    if (busy.current) return false;
    busy.current = true;
    setStatus(working);
    try {
      const text = await action();
      setStatus(text);
      return text === "";
    } finally {
      busy.current = false;
    }
  };
  return { status, run };
}

function Start() {
  const { create, restore } = useWorkspaceUploads();
  const { status, run } = useRun();
  const idField = useRef<HTMLInputElement>(null);
  const tokenField = useRef<HTMLInputElement>(null);
  const idLabel = useId();
  const tokenLabel = useId();

  const onRestore = async (): Promise<void> => {
    const id = idField.current?.value ?? "";
    const token = tokenField.current?.value ?? "";
    if (id.trim() === "" || token.trim() === "") return;
    if (await run(RESTORING, () => restore(id, token))) {
      if (idField.current) idField.current.value = "";
      if (tokenField.current) tokenField.current.value = "";
    }
  };

  return (
    <div>
      <p className="text-sm text-muted-foreground">{INTRO}</p>
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" onClick={() => void run(CREATING, create)}>
          {CREATE_BUTTON}
        </Button>
        <span className="text-sm text-muted-foreground">{HAVE_ONE}</span>
        <label htmlFor={idLabel} className="sr-only">
          {ID_LABEL}
        </label>
        <input id={idLabel} ref={idField} placeholder={ID_LABEL} autoComplete="off" spellCheck={false} className={FIELD} />
        <label htmlFor={tokenLabel} className="sr-only">
          {TOKEN_LABEL}
        </label>
        <input id={tokenLabel} ref={tokenField} type="password" placeholder={TOKEN_LABEL} autoComplete="off" className={FIELD} />
        <Button type="button" variant="outline" onClick={() => void onRestore()}>
          {RESTORE_BUTTON}
        </Button>
      </div>
      <output className="mt-2 block text-sm text-muted-foreground">
        {status}
      </output>
    </div>
  );
}

export function Workspace() {
  const enabled = useStats()?.uploads_enabled === true; // the one stats store: no second GET when the Ask page already loaded it
  const { workspace } = useWorkspace();
  useEffect(() => {
    if (getStats() === null) void refreshStats();
  }, []);
  return (
    <>
      <PageHeading title="Workspace" />
      {enabled ? (
        <section className="rounded-xl bg-card p-5 ring-1 ring-foreground/10">
          <h2 className="mb-2 text-lg font-semibold">{PANEL_TITLE}</h2>
          {workspace === null ? <Start /> : <WorkspaceActive key={workspace.id} session={workspace} />}
        </section>
      ) : null}
    </>
  );
}
