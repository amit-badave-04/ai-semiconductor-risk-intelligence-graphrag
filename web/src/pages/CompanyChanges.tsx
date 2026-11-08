import type { RiskChangesResult, RiskItem, RiskKind, RiskPair, RiskPassage } from "@/lib/company";
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
  pairHeader,
  quoted,
} from "./Company.copy";

// R25 (owner 2026-11-08: yes): the recent risk changes of the company. Everything from the server is a text node (S2): a
// headline, a quote and a reason are strings, a filing-backed passage shows its chunk id as text with a data-cite attribute (the
// same element as the dossier's), and no href, src or style comes from data.

export type RiskChangesState = RiskChangesResult | null;

// The order the labels read in: what is new, what no longer appears separately, what the check could not settle, what is reworded.
const KIND_ORDER: readonly RiskKind[] = ["new", "dropped", "unsettled", "changed"];
const KIND_LABEL: Record<RiskKind, string> = {
  new: KIND_NEW,
  dropped: KIND_DROPPED,
  unsettled: KIND_UNSETTLED,
  changed: KIND_CHANGED,
};

function Passage({ passage }: { passage: RiskPassage }) {
  return (
    <p className="my-1 text-sm">
      {passage.quote === null ? null : quoted(passage.quote)}
      {passage.chunkId === null ? null : (
        <>
          {passage.quote === null ? null : " "}
          <Cite id={passage.chunkId} />
        </>
      )}
    </p>
  );
}

function Item({ item }: { item: RiskItem }) {
  return (
    <li>
      {item.headline ?? ""}
      {item.passages.map((passage, index) => (
        <Passage key={index} passage={passage} />
      ))}
    </li>
  );
}

function Kind({ kind, items }: { kind: RiskKind; items: RiskItem[] }) {
  return (
    <>
      <p className="mt-2 mb-1 text-sm font-medium">{KIND_LABEL[kind]}</p>
      <ul className="ml-5 list-disc">
        {items.map((item, index) => (
          <Item key={index} item={item} />
        ))}
      </ul>
    </>
  );
}

function Pair({ pair }: { pair: RiskPair }) {
  const header = pairHeader(pair.older, pair.newer);
  if (!pair.compared) return <p className="mb-4 text-sm text-muted-foreground">{notComparedLine(header, pair.reason)}</p>;
  return (
    <div className="mb-4">
      <h3 className="text-sm font-semibold">{header}</h3>
      {KIND_ORDER.map((kind) => {
        const items = pair.items.filter((item) => item.kind === kind);
        return items.length === 0 ? null : <Kind key={kind} kind={kind} items={items} />;
      })}
    </div>
  );
}

function Body({ state }: { state: RiskChangesState }) {
  if (state === null) return <p className="text-muted-foreground">{MSG_LOADING}</p>;
  if (state.kind === "error") return <p className="text-destructive">{state.detail}</p>;
  if (state.kind === "notFound" || state.changes.pairs.length === 0) return <p className="text-muted-foreground">{NONE}</p>;
  return (
    <>
      {state.changes.pairs.map((pair, index) => (
        <Pair key={index} pair={pair} />
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
