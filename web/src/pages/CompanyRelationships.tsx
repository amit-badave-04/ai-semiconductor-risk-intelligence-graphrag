// docs/specs/m5b-redesign.md RD24: relationships without a relation name (lib/companyView.ts relationshipsView). Relations with a
// direction come first: the other company (a link to its page when it is one of the 13 filers, text otherwise), the quote "In the
// filing's own words" with the chip of the passage it came from beside it, then any other passages. The competitors follow as
// one list of names, each with its chips. Every value from the server is a text node (S2).
import { Link } from "react-router";
import { deck } from "@/copy/deck";
import { numberOccurrences } from "@/lib/citations";
import { CITE_GROUP, type Edge } from "@/lib/company";
import { relationshipsView } from "@/lib/companyView";
import { FILERS } from "@/lib/pages.generated";
import { Block, Cite } from "./CompanySection";

const NONE = deck.company.status.none;
const TICKER_BY_NAME: ReadonlyMap<string, string> = new Map(FILERS.map((f) => [f.name, f.ticker]));

function CompanyName({ name }: { name: string }) {
  const ticker = TICKER_BY_NAME.get(name);
  if (ticker === undefined) return <span>{name}</span>;
  return (
    <Link to={`/company/${ticker}/`} className="text-primary underline underline-offset-4">
      {name}
    </Link>
  );
}

function Names({ names }: { names: readonly string[] }) {
  return (
    <>
      {names.map((name, i) => (
        <span key={`${name}-${i}`}>
          {i > 0 && ", "}
          <CompanyName name={name} />
        </span>
      ))}
    </>
  );
}

function Chips({ ids, nths }: { ids: readonly string[]; nths: readonly number[] }) {
  return (
    <>
      {ids.map((id, j) => (
        <Cite key={`${id}:${nths[j]}`} id={id} group={CITE_GROUP.edges} nth={nths[j]} />
      ))}
    </>
  );
}

export function Relationships({ edges, companyName }: { edges: Edge[]; companyName: string }) {
  const w = deck.company.relationships;
  const view = relationshipsView(edges, companyName);
  // one count for the whole block, in reading order: each relation's quote chip and other chips, then the competitors' chips
  const groups = [...view.directional.map((r) => (r.quoteChunk === null ? r.chips : [r.quoteChunk, ...r.chips])), ...view.competitors.map((c) => c.chips)];
  const nths = numberOccurrences(groups);
  return (
    <Block id="relationships" title={w.heading}>
      {edges.length === 0 && <p className="text-muted-foreground">{NONE}</p>}
      {view.directional.length > 0 && (
        <ul className="m-0 list-none space-y-4 p-0">
          {view.directional.map((r, i) => {
            // the quote's own chip sits beside the quote; with no quote to sit beside, it leads the other passages
            const beside = r.quote !== null && r.quoteChunk !== null;
            const all = r.quoteChunk === null ? r.chips : [r.quoteChunk, ...r.chips];
            const rest = beside ? all.slice(1) : all;
            const restNths = beside ? nths[i].slice(1) : nths[i];
            return (
              <li key={`${r.other.join("|")}-${i}`} className="space-y-1">
                <p className="m-0 font-semibold break-words">
                  <Names names={r.other} />
                </p>
                {r.quote !== null && (
                  <>
                    <p className="m-0 text-sm text-muted-foreground">{w.ownWords}</p>
                    <div className="flex flex-wrap items-start gap-2">
                      <blockquote className="m-0 min-w-0 flex-1 border-l-4 border-border-strong pl-3 font-serif break-words">{r.quote}</blockquote>
                      {beside && <Cite id={r.quoteChunk as string} group={CITE_GROUP.edges} nth={nths[i][0]} />}
                    </div>
                  </>
                )}
                {rest.length > 0 && (
                  <p className="m-0 flex flex-wrap gap-2">
                    <Chips ids={rest} nths={restNths} />
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {view.competitors.length > 0 && (
        <>
          <h3 className="mt-6 mb-2 text-base font-semibold">{w.competitors}</h3>
          <ul className="m-0 list-none space-y-2 p-0">
            {view.competitors.map((c, i) => (
              <li key={`${c.name}-${i}`} className="flex flex-wrap items-center gap-2">
                <span className="font-semibold break-words">
                  <CompanyName name={c.name} />
                </span>
                <Chips ids={c.chips} nths={nths[view.directional.length + i]} />
              </li>
            ))}
          </ul>
        </>
      )}
    </Block>
  );
}
