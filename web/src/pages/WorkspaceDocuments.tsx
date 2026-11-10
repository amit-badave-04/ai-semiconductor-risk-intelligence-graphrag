// R28, A4 (docs/specs/m5b-frontend.md): the documents of the open workspace: each one's version timeline, a button per version that
// asks for what changed since the one before it, and the "ask as of vN" selector (index.html:900-965). Titles come from the
// uploader, so they are text nodes. The as-of value is the provider's single `asOf`: the date field and these selectors write the
// same value, so at most one choice is ever active, and the selector shown is derived from it (no copy of the state to drift).
import { useId } from "react";
import { versionAskAsOfOptions, versionTimeline, type WsDocument } from "@/lib/workspace";
import { NO_DOCUMENTS } from "./Workspace.copy";

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

interface DocumentsProps {
  documents: readonly WsDocument[];
  asOf: string;
  setAsOf: (value: string) => void;
  onShowChanges: (document: WsDocument, version: number) => void;
}

/** The id of the first document whose versions offer the active as-of instant: only it shows the choice (legacy C3). */
function pinnedDocument(documents: readonly WsDocument[], asOf: string): string | null {
  if (asOf === "" || DATE_ONLY.test(asOf)) return null;
  return documents.find((d) => d.versions.some((v) => v.created_at === asOf))?.document_id ?? null;
}

type RowProps = Omit<DocumentsProps, "documents"> & { doc: WsDocument; pinned: boolean };

function DocumentRow({ doc, pinned, asOf, setAsOf, onShowChanges }: RowProps) {
  const selectId = useId();
  const name = doc.title || doc.document_id;
  return (
    <li className="border-b border-border py-3">
      <p className="m-0">
        <strong>{name}</strong>
        {doc.latest_version === null ? null : ` (latest v${doc.latest_version})`}
      </p>
      <p className="m-0 mt-1 flex flex-wrap items-center gap-1 text-sm text-muted-foreground">
        <span>{versionTimeline(doc.versions)}</span>
        <span>{"\u00b7"}</span>
        {doc.versions.map((v) => (
          <button
            key={v.version}
            type="button"
            aria-label={`${name}: v${v.version}`}
            onClick={() => onShowChanges(doc, v.version)}
            className="rounded-md border border-border px-2 py-0.5 hover:bg-muted"
          >
            {`v${v.version}`}
          </button>
        ))}
      </p>
      <label htmlFor={selectId} className="mt-1 block text-sm text-muted-foreground">
        {`${name}: `}
        <select
          id={selectId}
          value={pinned ? asOf : ""}
          onChange={(event) => {
            const value = event.target.value;
            // choosing "current" ends an instant, but leaves a date the visitor typed
            if (value !== "" || !DATE_ONLY.test(asOf)) setAsOf(value);
          }}
          className="h-8 rounded-md border border-input bg-transparent px-2 text-sm"
        >
          {versionAskAsOfOptions(doc.versions).map((o, index) => (
            <option key={index} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </label>
    </li>
  );
}

export function WorkspaceDocuments({ documents, asOf, setAsOf, onShowChanges }: DocumentsProps) {
  if (documents.length === 0) return <p className="mt-3 text-sm text-muted-foreground">{NO_DOCUMENTS}</p>;
  const pinned = pinnedDocument(documents, asOf);
  return (
    <ul className="m-0 mt-3 list-none p-0">
      {documents.map((doc) => (
        <DocumentRow key={doc.document_id} doc={doc} pinned={pinned === doc.document_id} asOf={asOf} setAsOf={setAsOf} onShowChanges={onShowChanges} />
      ))}
    </ul>
  );
}
