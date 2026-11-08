// A5 / A2: where focus goes when the evidence drawer closes and the chip that opened it is gone (a new answer replaced it). The Ask
// page's answer heading carries this id and tabindex="-1"; with no such heading the page's h1 takes the focus. Never <body>.
export const ANSWER_HEADING_ID = "ask-heading";

/** The element focus falls back to: the answer heading, else the h1 of the page, else null. */
export function fallbackFocusTarget(): HTMLElement | null {
  return document.getElementById(ANSWER_HEADING_ID) ?? document.querySelector<HTMLElement>("main h1");
}
