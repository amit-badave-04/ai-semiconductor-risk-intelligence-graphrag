// R22: the badges under an answer and the whole footer of an answer. Every string comes from lib/answerMeta.ts and is rendered as a
// text node (S2, S11); status is never colour alone (A8): each badge carries its words.
import { metaSummary, type Badge as BadgeData, type BadgeClass } from "@/lib/answerMeta";
import { ChecksNotes } from "./Checks";

const TONE: Record<BadgeClass, string> = {
  "": "",
  ok: "text-green-800 dark:text-green-400",
  warn: "text-amber-800 dark:text-amber-400",
  bad: "text-red-700 dark:text-red-400",
};

export function Badge({ badge }: { badge: BadgeData }) {
  return (
    <span data-badge={badge.cls || undefined} className={`mr-1.5 mb-1 inline-block rounded-full bg-muted px-2.5 py-0.5 text-xs ${TONE[badge.cls]}`}>
      {badge.text}
    </span>
  );
}

export interface AnswerMetaProps {
  /** The `done` event (or any object: a malformed one never throws). */
  done: unknown;
  /** The configured answer model from /api/stats, for the streamed-live badge. */
  defaultModel?: unknown;
}

/** The footer of a finished answer: usage and cost or the cached note, the answer badge, the checks badges, the warnings, the hint. */
export function AnswerMeta({ done, defaultModel }: AnswerMetaProps) {
  const summary = metaSummary(done, defaultModel);
  return (
    <div className="mt-3.5 border-t border-border pt-2.5 text-sm text-muted-foreground">
      <div>
        {summary.badges.map((badge, index) => (
          <Badge key={index} badge={badge} />
        ))}
      </div>
      <ChecksNotes warnings={summary.warnings} hint={summary.hint} />
    </div>
  );
}
