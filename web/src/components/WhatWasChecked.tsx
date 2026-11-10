// docs/specs/m5b-redesign.md RD14: "What was checked" under a finished answer, in plain sentences (lib/answerMeta.ts checkedView,
// worded by copy/deck.ts checked). Each line has an icon whose SHAPE says ok, caution, problem or neutral (never colour alone; the
// icon is hidden from assistive technology because the sentence says the same). Server values arrive inside the sentences as text.
import { CircleCheck, CircleMinus, CircleX, TriangleAlert, type LucideIcon } from "lucide-react";
import { useId } from "react";
import { deck } from "@/copy/deck";
import { checkedView, type CheckedLine } from "@/lib/answerMeta";
import type { DoneEvent } from "@/lib/events";

const TONES: Record<CheckedLine["tone"], { icon: LucideIcon; colour: string }> = {
  ok: { icon: CircleCheck, colour: "text-primary" },
  warn: { icon: TriangleAlert, colour: "text-caution" },
  bad: { icon: CircleX, colour: "text-destructive" },
  plain: { icon: CircleMinus, colour: "text-muted-foreground" },
};

export function WhatWasChecked({ done }: { done: DoneEvent }) {
  const headingId = useId();
  const { lines } = checkedView(done);
  if (lines.length === 0) return null;
  return (
    <section aria-labelledby={headingId} className="mt-6 border-t border-border pt-4">
      <h3 id={headingId} className="m-0 mb-2 text-base font-semibold">
        {deck.checked.heading}
      </h3>
      <ul className="m-0 list-none space-y-1.5 p-0">
        {lines.map((line, index) => {
          const { icon: Icon, colour } = TONES[line.tone];
          return (
            <li key={`${line.code}-${index}`} className="flex items-start gap-2">
              <Icon aria-hidden="true" className={`mt-0.5 size-4 shrink-0 ${colour}`} />
              <span>{deck.checked.line(line)}</span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
