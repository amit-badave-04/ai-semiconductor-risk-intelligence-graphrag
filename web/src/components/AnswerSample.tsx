// docs/specs/m5b-redesign.md RD6: "What an answer looks like", a static sample beside the question box on wide screens (below it
// on a phone): a sentence semigraph wrote, the source stamp it cites, the cited passage with the words copied exactly from the
// filing highlighted, the source line and a one-line legend. Nothing in it is interactive, so it is never mistaken for an answer.
import { FileText } from "lucide-react";
import { useId } from "react";
import { deck } from "@/copy/deck";

export function AnswerSample() {
  const w = deck.sample;
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="rounded-xl border border-border bg-card p-4 text-sm">
      <h2 id={headingId} className="m-0 mb-2 text-sm font-semibold tracking-wide text-muted-foreground uppercase">
        {w.heading}
      </h2>
      <p className="m-0 text-base">
        {w.answer}{" "}
        <span className="inline-flex items-center gap-1 rounded-md border border-brand-line bg-brand-fill px-1.5 align-baseline text-sm font-semibold whitespace-nowrap text-primary">
          <FileText aria-hidden="true" className="size-3.5 shrink-0" />
          {w.stamp}
        </span>
      </p>
      <blockquote className="m-0 mt-3 border-l-4 border-mark-edge pl-3 font-serif text-base leading-relaxed">
        <mark>{w.quote}</mark>
        {w.quoteAfter}
      </blockquote>
      <p className="m-0 mt-2 text-muted-foreground">{w.source}</p>
      <p className="m-0 mt-2 flex items-center gap-2 text-muted-foreground">
        <span aria-hidden="true" className="inline-block h-3 w-5 rounded-sm border border-mark-edge bg-mark" />
        {w.legend}
      </p>
    </section>
  );
}
