// docs/specs/m5b-redesign.md RD13 and RD15, under a finished answer. "Read with care": one note per pinned change-claim wording
// the answer holds (lib/hedge.ts), each with that wording's own hand-check counts (pages/Method.copy.ts HEDGES), and a link to
// the Method page's reliability section. "Sources (N)": the answer's distinct cited sources (lib/answerSources.ts, the same
// parse as the chips), each a chip in its own place ("sources"), so every chip key on the page stays unique (A2). The answer is
// untrusted text: it is only searched and parsed here, never rendered as markup.
import { TriangleAlert } from "lucide-react";
import { useId, useMemo } from "react";
import { Link } from "react-router";
import { deck } from "@/copy/deck";
import { answerSources } from "@/lib/answerSources";
import { hedgesIn } from "@/lib/hedge";
import { HEDGES } from "@/pages/Method.copy";
import { Chip } from "./Chips";

export function ReadWithCare({ text }: { text: string }) {
  const headingId = useId();
  const groups = useMemo(() => hedgesIn(text), [text]);
  if (groups.length === 0) return null;
  const w = deck.care;
  return (
    <section aria-labelledby={headingId} className="mt-6 rounded-md border border-caution-line bg-caution-fill px-4 py-3 text-caution">
      <h3 id={headingId} className="m-0 mb-1 flex items-center gap-2 text-base font-semibold">
        <TriangleAlert aria-hidden="true" className="size-4 shrink-0" />
        {w.heading}
      </h3>
      <ul className="m-0 list-none space-y-1 p-0 text-sm">
        {groups.map((group) => (
          <li key={group}>{w.line(group, HEDGES[group].counts)}</li>
        ))}
      </ul>
      <p className="m-0 mt-2 text-sm">
        <Link to="/method/#method-reliability" className="underline underline-offset-4">
          {w.link}
        </Link>
      </p>
    </section>
  );
}

export function SourcesList({ text, gen, staleIds }: { text: string; gen: number; staleIds: readonly string[] }) {
  const headingId = useId();
  const ids = useMemo(() => answerSources(text), [text]);
  if (ids.length === 0) return null;
  return (
    <section aria-labelledby={headingId} className="mt-6">
      <h3 id={headingId} className="m-0 mb-2 text-base font-semibold">
        {deck.chips.sourcesHeading(ids.length)}
      </h3>
      <ul className="m-0 list-none space-y-1.5 p-0">
        {ids.map((id) => (
          <li key={id}>
            <Chip id={id} place="sources" gen={gen} nth={0} stale={staleIds.includes(id)} />
          </li>
        ))}
      </ul>
    </section>
  );
}
