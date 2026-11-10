// docs/specs/m5b-redesign.md RD17 and RD18: the evidence panel's body, over lib/evidenceView.ts evidenceBody. The title; Section /
// Filed / Status; an "Older filing" band when the filing was replaced; the passage in serif, with the words the answer quoted
// marked where the passage holds them exactly (lib/highlight.ts; text nodes inside <mark>, a legend only when something is
// marked); the source link (safeUrl); and "Details for analysts" with the ids, in a native disclosure. Every value is a text node.
import { Landmark } from "lucide-react";
import { deck } from "@/copy/deck";
import { evidenceBody, type EvidenceBody as Body } from "@/lib/evidenceView";
import { highlight } from "@/lib/highlight";
import { SAFE_REL } from "@/lib/safeUrl";
import type { Content } from "./useDrawerController";

const w = deck.evidence;

function Passage({ body, quotes }: { body: Body; quotes: readonly string[] }) {
  const marked = body.highlightable ? highlight(body.passage, quotes) : { segments: [{ text: body.passage, marked: false }], marked: false };
  return (
    <>
      <blockquote className="m-0 mt-3 border-l-4 border-border-strong pl-3 font-serif text-base leading-relaxed whitespace-pre-wrap">
        {marked.segments.map((segment, index) => (segment.marked ? <mark key={index}>{segment.text}</mark> : segment.text))}
      </blockquote>
      {marked.marked && (
        <p className="m-0 mt-2 flex items-center gap-2 text-sm text-muted-foreground">
          <span aria-hidden="true" className="inline-block h-3 w-5 rounded-sm border border-mark-edge bg-mark" />
          {deck.sample.legend}
        </p>
      )}
    </>
  );
}

function Loaded({ id, payload, quotes }: { id: string; payload: unknown; quotes: readonly string[] }) {
  const body = evidenceBody(id, payload);
  return (
    <>
      {body.rule && (
        <p className="m-0 mb-1 inline-flex items-center gap-1 rounded-sm border border-rule-line bg-rule-fill px-1.5 text-sm font-semibold text-rule">
          <Landmark aria-hidden="true" className="size-4" />
          {w.ruleLabel}
        </p>
      )}
      <h3 className="m-0 text-lg font-semibold">{body.heading === "" ? w.untitled : body.heading}</h3>
      {body.facts.length > 0 && (
        <dl className="m-0 mt-2 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
          {body.facts.map((fact) => (
            <div key={fact.code} className="contents">
              <dt className="text-muted-foreground">{w.fact[fact.code]}</dt>
              <dd className="m-0 break-words">{fact.code === "status" ? w.status(fact.value) : fact.value}</dd>
            </div>
          ))}
        </dl>
      )}
      {body.superseded !== null && (
        <p className="m-0 mt-3 rounded-md border border-caution-line bg-caution-fill px-3 py-2 text-sm text-caution">
          {body.kind === "doc" ? w.docBand : w.band(body.superseded.until)}
        </p>
      )}
      {body.figure !== null && (
        <p className="m-0 mt-3">
          <span className="text-2xl font-semibold">{body.figure.compact}</span>{" "}
          <span className="text-muted-foreground">{w.exactly(body.figure.exact)}</span>
        </p>
      )}
      {body.passage !== "" && <Passage body={body} quotes={quotes} />}
      {body.rule && <p className="m-0 mt-2 text-sm text-muted-foreground">{w.ruleNote}</p>}
      {body.kind === "doc" && <p className="m-0 mt-2 text-sm text-muted-foreground">{w.docNote}</p>}
      {body.link !== null && (
        <p className="m-0 mt-3">
          <a className="text-primary underline underline-offset-4" href={body.link.href} target="_blank" rel={SAFE_REL}>
            {w.link[body.link.code]}
          </a>
        </p>
      )}
      <details className="mt-4 text-sm">
        <summary className="cursor-pointer font-semibold text-muted-foreground">{w.details}</summary>
        <ul className="m-0 mt-1 list-none p-0 font-mono text-xs break-all text-muted-foreground">
          {body.details.map((detail) => (
            <li key={detail.code}>{`${w.detail[detail.code]}: ${detail.value}`}</li>
          ))}
        </ul>
      </details>
    </>
  );
}

/** The body of the open panel: what loads, what failed, or the evidence. */
export function EvidenceBody({ id, content, quotes }: { id: string; content: Content; quotes: readonly string[] }) {
  // announced politely (R2 review): focus is on Close while the source loads
  if (content.phase === "loading") return <output className="block text-muted-foreground">{w.loading}</output>;
  if (content.phase === "error") return <output className="block text-destructive">{w.failed(content.message)}</output>;
  return <Loaded id={id} payload={content.payload} quotes={quotes} />;
}
