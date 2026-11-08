import { afterEach, describe, expect, it } from "vitest";
import { ANSWER_HEADING_ID, fallbackFocusTarget } from "../focusTargets";

// Built with DOM calls, never innerHTML (S1: the authored source has no HTML sink, tests included).
function page(...children: HTMLElement[]): void {
  const main = document.createElement("main");
  main.append(...children);
  document.body.replaceChildren(main);
}
function element(tag: string, text: string, id?: string): HTMLElement {
  const el = document.createElement(tag);
  el.textContent = text;
  if (id !== undefined) el.id = id;
  return el;
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("fallbackFocusTarget (A5/A2: never <body>)", () => {
  it("is the answer heading when the page has one", () => {
    page(element("h1", "Page"), element("h2", "Answer", ANSWER_HEADING_ID));
    expect(fallbackFocusTarget()?.id).toBe(ANSWER_HEADING_ID);
  });

  it("is the h1 of the main landmark when there is no answer heading", () => {
    page(element("h1", "Page"));
    expect(fallbackFocusTarget()?.tagName).toBe("H1");
  });

  it("is null when the page has neither", () => {
    page(element("p", "nothing"));
    expect(fallbackFocusTarget()).toBeNull();
  });
});
