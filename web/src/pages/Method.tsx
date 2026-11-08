import type { ReactNode } from "react";
import { Link } from "react-router";
import { PageHeading } from "@/components/Layout";
import { CHECKS_HINT, INTRO, RELIABILITY, type Segment, WITHDRAWN } from "./Method.copy";

// D7 (docs/specs/m5b-frontend.md): the Method & Evidence page. The sentences are the pinned copy of Method.copy.ts; this file
// adds only the two section headings (listed in the handoff for the owner's review): the withdrawn-accuracy notice is an
// unheaded note, as on the legacy page (owner 2026-11-08). It makes no request. Its external links are the static ones of the
// copy (S2), opened with rel="noopener noreferrer"; the one other link is the route link to the Freshness page (R27, owner
// 2026-11-08), labelled as the navigation labels it.

function Runs({ segments }: { segments: readonly Segment[] }) {
  return (
    <>
      {segments.map((s, i) => {
        if (s.href !== undefined) {
          return (
            <a key={i} className="text-primary underline underline-offset-4" href={s.href} target="_blank" rel="noopener noreferrer">
              {s.text}
            </a>
          );
        }
        return s.bold ? <b key={i}>{s.text}</b> : s.text;
      })}
    </>
  );
}

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="mb-8">
      <h2 id={id} className="mb-2 text-lg font-semibold">
        {title}
      </h2>
      {children}
    </section>
  );
}

export function Method() {
  const [lead, ...rest] = RELIABILITY;
  return (
    <>
      <PageHeading title="Method & Evidence" />
      <p className="mb-8 max-w-[75ch]">
        <Runs segments={INTRO} />
      </p>
      <p className="mb-6 max-w-[75ch] text-sm text-muted-foreground">
        <Runs segments={WITHDRAWN} />
      </p>
      <p className="mb-8">
        <Link
          to="/freshness/"
          className="rounded-sm text-primary underline underline-offset-4 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
        >
          Freshness
        </Link>
      </p>
      <Section id="method-checks" title="What the checks mean">
        <p className="max-w-[75ch]">{CHECKS_HINT}</p>
      </Section>
      {/* Last on the page: the checks hint above says the note is at the bottom of the page. */}
      <Section id="method-reliability" title={lead?.text ?? ""}>
        <p className="max-w-[75ch]">
          <Runs segments={rest} />
        </p>
      </Section>
    </>
  );
}
