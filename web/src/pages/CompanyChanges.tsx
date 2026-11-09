import { numberOccurrences } from "@/lib/citations";
import { CITE_GROUP, type RiskChangesResult, type RiskItem, type RiskKind, type RiskPair, type RiskPassage } from "@/lib/company";
import { Block, Cite } from "./CompanyBlocks";
import {
  KIND_CHANGED,
  KIND_DROPPED,
  KIND_NEW,
  KIND_UNSETTLED,
  MSG_LOADING,
  NONE,
  RISK_CHANGES_TITLE,
  notComparedLine,
  passageLabel,
  pairHeader,
  quoted,
} from "./Company.copy";

// R25 (owner 2026-11-08: yes): the recent risk changes of the company. Everything from the server is a text node (S2): a
// headline, a quote and a reason are strings, a filing-backed passage's chunk id is the evidence chip (Cite, the same element as the
// dossier's: it opens the one drawer), and no href, src or style comes from data.

export type RiskChangesState = RiskChangesResult | null;

/** The occurrence number of each passage's chunk id across all pairs, in data order (a chip key part, A2). */
type PassageNumbers = ReadonlyMap<RiskPassage, number>;

function numberPassages(pairs: readonly RiskPair[]): PassageNumbers {
  const passages = pairs.flatMap((pair) => pair.items.flatMap((item) => item.passages));
  const nths = numberOccurrences(passages.map((p) => (p.chunkId === null ? [] : [p.chunkId])));
  return new Map(passages.map((p, i) => [p, nths[i][0] ?? 0]));
}

// The order the labels read in: what is new, what no longer appears separately, what the check could not settle, what is reworded.
const KIND_ORDER: readonly RiskKind[] = ["new", "dropped", "unsettled", "changed"];
const KIND_LABEL: Record<RiskKind, string> = {
  new: KIND_NEW,
  dropped: KIND_DROPPED,
  unsettled: KIND_UNSETTLED,
  changed: KIND_CHANGED,
};

// Owner 2026-11-08: a passage names its side by that side's filing, so an older and a newer wording read apart.
interface PassageProps {
  passage: RiskPassage;
  pair: RiskPair;
  numbers: PassageNumbers;
}

function Passage({ passage, pair, numbers }: PassageProps) {
  return (
    <p className="my-1 text-sm">
      {passage.quote === null ? null : `${passageLabel(passage.side, pair.older, pair.newer)}${quoted(passage.quote)}`}
      {passage.chunkId === null ? null : (
        <>
          {passage.quote === null ? null : " "}
          <Cite id={passage.chunkId} group={CITE_GROUP.changes} nth={numbers.get(passage) ?? 0} />
        </>
      )}
    </p>
  );
}

function Item({ item, pair, numbers }: { item: RiskItem; pair: RiskPair; numbers: PassageNumbers }) {
  return (
    <li>
      {item.headline ?? ""}
      {item.passages.map((passage, index) => (
        <Passage key={index} passage={passage} pair={pair} numbers={numbers} />
      ))}
    </li>
  );
}

interface KindProps {
  kind: RiskKind;
  items: RiskItem[];
  pair: RiskPair;
  numbers: PassageNumbers;
}

function Kind({ kind, items, pair, numbers }: KindProps) {
  return (
    <>
      <p className="mt-2 mb-1 text-sm font-medium">{KIND_LABEL[kind]}</p>
      <ul className="ml-5 list-disc">
        {items.map((item, index) => (
          <Item key={index} item={item} pair={pair} numbers={numbers} />
        ))}
      </ul>
    </>
  );
}

function Pair({ pair, numbers }: { pair: RiskPair; numbers: PassageNumbers }) {
  const header = pairHeader(pair.older, pair.newer);
  if (!pair.compared) return <p className="mb-4 text-sm text-muted-foreground">{notComparedLine(header, pair.reason)}</p>;
  return (
    <div className="mb-4">
      <h3 className="text-sm font-semibold">{header}</h3>
      {KIND_ORDER.map((kind) => {
        const items = pair.items.filter((item) => item.kind === kind);
        return items.length === 0 ? null : <Kind key={kind} kind={kind} items={items} pair={pair} numbers={numbers} />;
      })}
    </div>
  );
}

function Body({ state }: { state: RiskChangesState }) {
  if (state === null) return <p className="text-muted-foreground">{MSG_LOADING}</p>;
  if (state.kind === "error") return <p className="text-destructive">{state.detail}</p>;
  if (state.kind === "notFound" || state.changes.pairs.length === 0) return <p className="text-muted-foreground">{NONE}</p>;
  const numbers = numberPassages(state.changes.pairs);
  return (
    <>
      {state.changes.pairs.map((pair, index) => (
        <Pair key={index} pair={pair} numbers={numbers} />
      ))}
    </>
  );
}

export function RiskChangesBlock({ state }: { state: RiskChangesState }) {
  return (
    <Block title={RISK_CHANGES_TITLE}>
      <Body state={state} />
    </Block>
  );
}
