// R28 (docs/specs/m5b-frontend.md) and RD26 (docs/specs/m5b-redesign.md, docs/decisions/002-r4-workspace-copy.md): the workspace
// page. Hidden until GET /api/stats says uploads are enabled (index.html:633): with them off, or while that is unknown, the page is
// its heading alone and makes no other request. The first-time view says what a workspace is, who can open it, when it is deleted
// and what is sent, lists three steps and the accepted types, and keeps Restore behind a disclosure. Create, restore and delete are
// handlers over the provider (state/WorkspaceProvider.tsx), which owns the session, the documents and the upload queue, so a route
// change loses none of it.
//   * One action at a time across the panel (one `useRun`), and one status region that never unmounts while the panel is shown, so
//     a result is announced and Create and Restore cannot run together (plan D4).
//   * Focus goes to the "Your documents" h2 after create or restore, and to Create after delete.
//   * The restore key is read from an UNCONTROLLED password field inside the disclosure and cleared after use, so it never sits in a
//     `value` attribute or in React state (S8); closing the disclosure unmounts the field and forgets what was typed.
import { Collapsible } from "@base-ui/react/collapsible";
import { ChevronRight } from "lucide-react";
import { useEffect, useId, useRef, useState, type RefObject } from "react";
import { PageHeading } from "@/components/Layout";
import { Button } from "@/components/ui/button";
import { deck } from "@/copy/deck";
import { getStats, refreshStats, useStats } from "@/lib/statsStore";
import { useWorkspace } from "@/state/useWorkspace";
import { useWorkspaceUploads } from "@/state/useWorkspaceUploads";
import { WorkspaceActive } from "./WorkspaceActive";

const W = deck.workspace;

const FIELD = "h-9 w-full min-w-0 rounded-md border border-input bg-transparent px-2.5 text-sm";

/** Starts an action unless one is running; true when it ended with no message (the action's success). */
type Run = (working: string, action: () => Promise<string>) => Promise<boolean>;

/** One action at a time (a double click sends one request), with the status text it shows: the working text, then the result. */
function useRun(): { status: string; run: Run } {
  const [status, setStatus] = useState("");
  const busy = useRef(false);
  const run: Run = async (working, action) => {
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

function WorkspaceIntro() {
  return (
    <>
      <p className="m-0">{W.what}</p>
      <ol className="mt-3 mb-0 list-decimal space-y-1 pl-6">
        {W.steps.map((step) => (
          <li key={step}>{step}</li>
        ))}
      </ol>
      <p className="mt-3 mb-0 text-sm text-muted-foreground">{W.types}</p>
      <p className="mt-3 mb-0 text-sm">{W.access}</p>
      <p className="mt-1 mb-0 text-sm">{W.deletion}</p>
      <p className="mt-1 mb-0 text-sm">{W.providers}</p>
    </>
  );
}

interface RestoreProps {
  run: Run;
  statusId: string;
  onRestored: () => void;
}

function RestoreDisclosure({ run, statusId, onRestored }: RestoreProps) {
  const { restore } = useWorkspaceUploads();
  const idField = useRef<HTMLInputElement>(null);
  const keyField = useRef<HTMLInputElement>(null);
  const idLabel = useId();
  const keyLabel = useId();

  const onRestore = async (): Promise<void> => {
    const id = idField.current?.value ?? "";
    const key = keyField.current?.value ?? "";
    if (id.trim() === "" || key.trim() === "") return;
    if (await run(W.RESTORING, () => restore(id, key))) {
      if (idField.current) idField.current.value = "";
      if (keyField.current) keyField.current.value = "";
      onRestored();
    }
  };

  return (
    <Collapsible.Root className="mt-4">
      <Collapsible.Trigger className="group inline-flex min-h-11 items-center gap-1.5 text-sm font-semibold">
        <ChevronRight aria-hidden="true" className="size-4 group-data-[panel-open]:rotate-90" />
        {W.restoreTrigger}
      </Collapsible.Trigger>
      <Collapsible.Panel className="mt-1 pl-6">
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex w-full max-w-[22rem] min-w-0 flex-col gap-1">
            <label htmlFor={idLabel} className="text-sm">
              {W.idLabel}
            </label>
            <input id={idLabel} ref={idField} autoComplete="off" spellCheck={false} aria-describedby={statusId} className={FIELD} />
          </div>
          <div className="flex w-full max-w-[22rem] min-w-0 flex-col gap-1">
            <label htmlFor={keyLabel} className="text-sm">
              {W.keyLabel}
            </label>
            <input id={keyLabel} ref={keyField} type="password" autoComplete="off" aria-describedby={statusId} className={FIELD} />
          </div>
          <Button type="button" variant="outline" onClick={() => void onRestore()}>
            {W.RESTORE_BUTTON}
          </Button>
        </div>
      </Collapsible.Panel>
    </Collapsible.Root>
  );
}

/** Focus Create when the workspace closes after a delete (the button mounts with the first-time view). */
function useFocusCreateAfterDelete(open: boolean): { createButton: RefObject<HTMLButtonElement | null>; expectDelete: (on: boolean) => void } {
  const createButton = useRef<HTMLButtonElement>(null);
  const pending = useRef(false);
  useEffect(() => {
    if (open || !pending.current) return;
    pending.current = false;
    createButton.current?.focus();
  }, [open]);
  const expectDelete = (on: boolean): void => {
    pending.current = on;
  };
  return { createButton, expectDelete };
}

function Panel() {
  const { workspace } = useWorkspace();
  const { create, remove, announcement } = useWorkspaceUploads();
  const { status, run } = useRun();
  const heading = useRef<HTMLHeadingElement>(null);
  const statusId = useId();
  const { createButton, expectDelete } = useFocusCreateAfterDelete(workspace !== null);
  const opened = (): void => heading.current?.focus();

  const onCreate = async (): Promise<void> => {
    if (await run(W.CREATING, create)) opened();
  };
  const onDelete = async (): Promise<void> => {
    expectDelete(true);
    const done = await run("", async () => {
      await remove();
      return "";
    });
    if (!done) expectDelete(false);
  };

  return (
    <section className="rounded-xl bg-card p-5 ring-1 ring-foreground/10">
      <h2 ref={heading} tabIndex={-1} className="mb-2 text-lg font-semibold">
        {W.title}
      </h2>
      {workspace === null ? (
        <>
          <WorkspaceIntro />
          <Button ref={createButton} type="button" className="mt-4" onClick={() => void onCreate()}>
            {W.CREATE_BUTTON}
          </Button>
          <RestoreDisclosure run={run} statusId={statusId} onRestored={opened} />
        </>
      ) : (
        <WorkspaceActive key={workspace.id} session={workspace} onDelete={() => void onDelete()} />
      )}
      <output id={statusId} className={workspace === null ? "mt-2 block text-sm text-muted-foreground" : "sr-only"}>
        {workspace === null ? status : announcement}
      </output>
    </section>
  );
}

export function Workspace() {
  const enabled = useStats()?.uploads_enabled === true; // the one stats store: no second GET when the Ask page already loaded it
  useEffect(() => {
    if (getStats() === null) void refreshStats();
  }, []);
  return (
    <>
      <PageHeading title="Workspace" />
      {enabled ? <Panel /> : null}
    </>
  );
}
