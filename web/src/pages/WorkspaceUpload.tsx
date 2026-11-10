// R28, A4, A6, S12 (docs/specs/m5b-frontend.md): the upload area of the workspace page: the target selector, the drop zone and the
// rows of this visit's uploads. The drop zone is itself a real <button> carrying the legacy sentence, so the keyboard (Enter,
// Space) and the file picker work natively (A6) with no new copy; drag and drop land on it too. The hidden file input is named by
// that sentence. Files are handed to the provider's queue from the handlers below and nowhere else (S12).
import { useId, useRef, useState, type DragEvent } from "react";
import type { JobRow } from "@/lib/jobs";
import { deck } from "@/copy/deck";
import { FILE_ACCEPT, uploadTargetOptions, type WsDocument } from "@/lib/workspace";
import { useWorkspaceUploads } from "@/state/useWorkspaceUploads";

const { DROP_TEXT, UPLOAD_AS } = deck.workspace;

const TONE: Record<JobRow["tone"], string> = { "": "", ok: "text-foreground font-medium", err: "text-destructive" };

function Rows({ rows }: { rows: readonly JobRow[] }) {
  if (rows.length === 0) return null;
  return (
    <ul className="m-0 mt-3 list-none space-y-1 p-0 text-sm">
      {rows.map((row) => (
        <li key={row.key} data-job-row="" className="flex flex-wrap gap-x-3">
          <span className="font-medium break-all">
            <bdi>{row.name}</bdi>
          </span>
          <span className={TONE[row.tone]}>{row.text}</span>
        </li>
      ))}
    </ul>
  );
}

interface DropZoneProps {
  onFiles: (files: FileList | null | undefined) => void;
}

function DropZone({ onFiles }: DropZoneProps) {
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const dropId = useId();
  const over = (event: DragEvent): void => {
    event.preventDefault();
    setDragging(true);
  };
  return (
    <>
      <button
        type="button"
        id={dropId}
        data-dragging={dragging ? "" : undefined}
        onClick={() => input.current?.click()}
        onDragEnter={over}
        onDragOver={over}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          onFiles(event.dataTransfer.files);
        }}
        className="mt-3 w-full rounded-md border-2 border-dashed border-border px-4 py-6 text-center text-sm text-muted-foreground hover:bg-muted data-[dragging]:border-solid data-[dragging]:border-foreground data-[dragging]:bg-muted"
      >
        {DROP_TEXT}
      </button>
      <input
        ref={input}
        type="file"
        hidden
        accept={FILE_ACCEPT}
        aria-labelledby={dropId}
        onChange={(event) => {
          onFiles(event.target.files);
          event.target.value = ""; // choosing the same file again must fire a change again
        }}
      />
    </>
  );
}

export function WorkspaceUpload({ documents }: { documents: readonly WsDocument[] }) {
  const { upload, rows } = useWorkspaceUploads();
  const [chosen, setChosen] = useState("");
  const targetId = useId();
  const options = uploadTargetOptions(documents);
  // keep the choice across reloads of the list, as the legacy page does; a document that is gone falls back to a new one
  const target = options.some((o) => o.value === chosen) ? chosen : "";
  const hand = (files: FileList | null | undefined): void => {
    if (files && files.length > 0) upload(Array.from(files), target);
  };

  return (
    <div className="mt-3">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor={targetId} className="text-sm text-muted-foreground">
          {UPLOAD_AS}
        </label>
        <select
          id={targetId}
          value={target}
          onChange={(event) => setChosen(event.target.value)}
          className="h-9 max-w-full min-w-0 rounded-md border border-input bg-transparent px-2 text-sm"
        >
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      <DropZone onFiles={hand} />
      <Rows rows={rows} />
    </div>
  );
}
