// R22 / A3: the research agent's steps timeline. The polite live region and the accessible name are the legacy page's (pinned today by
// tests/test_serve_agent_ui.py); nothing moves (R30). The list is hidden until the first step; it shows at most MAX_STEPS_SHOWN steps
// and each is one list item of text.
import type { StepEvent } from "@/lib/events";
import { MAX_STEPS_SHOWN, stepText } from "@/lib/steps";
import { STEPS_LABEL } from "@/pages/Ask.copy";

export function Steps({ steps }: { steps: readonly StepEvent[] }) {
  const shown = steps.slice(0, MAX_STEPS_SHOWN);
  return (
    <div className="steps" aria-live="polite" aria-relevant="additions">
      <ol aria-label={STEPS_LABEL} hidden={shown.length === 0} className="mt-2 list-decimal pl-5.5 text-sm text-muted-foreground">
        {shown.map((step, index) => (
          <li key={index} className="my-0.5">
            {stepText(step)}
          </li>
        ))}
      </ol>
    </div>
  );
}
