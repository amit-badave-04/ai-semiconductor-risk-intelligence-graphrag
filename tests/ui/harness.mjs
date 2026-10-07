// Loads the inline <script> of src/semigraph/serve/static/index.html into a `vm` context with a minimal stub
// `document`, and returns the pure helper functions the page defines at the top of that script.
// No DOM library, no npm packages: `node --test` (Node >= 20) is all that is needed.
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));
export const INDEX_PATH = path.resolve(here, "../../src/semigraph/serve/static/index.html");

// The names the page's script must define. A missing one fails loudly instead of silently testing nothing.
const EXPORTS = [
  "esc", "shortModel", "classifyCitation", "chipLabel", "reasonText", "answerBadge", "checksSummary", "metaHtml",
  "renderMarkdown", "statsHtml", "limitsText", "costNote", "retrievalStatus", "safeUrl", "formatNumber",
  "evidenceView", "escalationStatus", "CITE", "checksPassed", "examplesHtml",
  "agentEnabled", "stepText", "tracingNote", "syncAgentUi", "addStep", "clearSteps",
  // M4: the upload workspace panel's pure functions (tests/ui/workspace.test.mjs)
  "docChipLabel", "staleClass", "jobStateText", "versionTimeline", "relativeTime", "freshnessLine",
  // M4 review fixes: upload target, title, "ask as of vN", and the richer what-changed view (findings 15, 16, 15.1)
  "uploadTargetOptions", "uploadTitleFor", "versionAskAsOfOptions", "changeItemHtml", "changesHtml",
  // Owner's live G10 test (2026-09-30), FIX 1: drop stale `doc:` evidence-cache entries on a new upload version.
  "dropDocEvidence",
  // Round-7 verification: a benchmark example click forces hybrid and must re-sync the agent hint at once.
  "askExample",
  // M5a closing review (V3): the answer stream's read loop, driven with a fake `fetch` (tests/ui/stream_cut.test.mjs).
  "ask",
];

function stubElement() {
  return {
    style: {}, dataset: {}, innerHTML: "", textContent: "", value: "", hidden: false, disabled: false,
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, appendChild() {}, setAttribute() {}, removeAttribute() {}, querySelectorAll() { return []; },
    closest() { return null; }, querySelector() { return null; }, remove() {},
    focus() {}, click() {}, contains() { return false; },   // the evidence drawer and the drop zone call these
  };
}

export function pageScript() {
  const html = readFileSync(INDEX_PATH, "utf8");
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  if (scripts.length !== 1) throw new Error(`expected exactly one inline <script>, found ${scripts.length}`);
  // The server substitutes the Turnstile site key; an empty key keeps the widget code inert in tests.
  return scripts[0][1].replace("__TURNSTILE_SITE_KEY__", "");
}

// `custom` lets a test supply its own elements: `{ elements: { id: element }, createElement: (tag) => element }`.
// It may also supply `fetch` (default: never settles). Besides `api` and `elements`, the result carries the `document`
// (its keydown/click listeners are recorded, and `document.dispatch(type, event)` fires them) and the vm `context`
// (a test can replace a page-level function there, e.g. `context.handleFiles = spy`).
export function loadPage(custom = {}) {
  const elements = new Map();
  const listeners = {};
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, (custom.elements || {})[id] || stubElement());
      const element = elements.get(id);
      if (element instanceof FakeElement && !element.ownerDocument) element.ownerDocument = document;
      return element;
    },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    dispatch(type, event = {}) {
      const e = { type, key: "", defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...event };
      for (const fn of listeners[type] || []) fn(e);
      return e;
    },
    createElement(tag) { return custom.createElement ? custom.createElement(tag) : stubElement(); },
    querySelectorAll() { return []; },
    head: { appendChild() {} }, body: stubElement(),
  };
  document.activeElement = document.body;
  // fetch never settles unless a test says otherwise: the page's start-up calls (stats, examples) must not resolve or
  // reject during a test.
  const sandbox = { document, window: {}, fetch: custom.fetch || (() => new Promise(() => {})), console, TextDecoder };
  const context = vm.createContext(sandbox);
  const api = vm.runInContext(`${pageScript()}\n;({ ${EXPORTS.join(", ")} })`, context);
  return { api, elements, document, context };
}

// ---- A small focus-aware DOM model for the keyboard tests (tests/ui/a11y.test.mjs). It models only what those tests
// rely on: attributes, classList, listeners, `.click()`, and which elements the keyboard can reach. An element is
// focusable when it is natively interactive (or has tabindex >= 0), is not `hidden` or display:none, and neither it nor
// an ancestor carries the `inert` attribute. `aria-hidden` removes an element from the accessibility tree only: it does
// NOT stop focus (that mismatch is exactly the bug the drawer had), so it is modelled separately in hiddenFromAT().
const NATIVELY_FOCUSABLE = new Set(["button", "a", "input", "select", "textarea"]);

export class FakeElement {
  constructor(id = "", { tag = "div", attrs = {}, parent = null } = {}) {
    this.id = id; this.tag = tag; this.parent = parent; this.ownerDocument = null;
    this.style = {}; this.dataset = {}; this.innerHTML = ""; this.textContent = ""; this.value = ""; this.hidden = false;
    this.disabled = false; this.files = []; this.clicks = 0;
    this.attrs = new Map(Object.entries(attrs));
    this.listeners = {};
    const classes = new Set(String(attrs.class || "").split(/\s+/).filter(Boolean));
    this.classList = {
      add: (c) => { classes.add(c); }, remove: (c) => { classes.delete(c); },
      toggle: (c, force) => { if (force === undefined ? !classes.has(c) : force) classes.add(c); else classes.delete(c); },
      contains: (c) => classes.has(c),
    };
  }
  setAttribute(name, value) { this.attrs.set(name, String(value)); this.#fixFocus(); }
  removeAttribute(name) { this.attrs.delete(name); this.#fixFocus(); }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  hasAttribute(name) { return this.attrs.has(name); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, event = {}) {
    const e = { type, target: this, key: "", defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...event };
    for (const fn of this.listeners[type] || []) fn(e);
    return e;
  }
  click() { this.clicks += 1; this.dispatch("click"); }
  closest(selector) {
    const cls = selector.startsWith(".") ? selector.slice(1) : null;
    for (let el = this; el; el = el.parent) if (cls && el.classList.contains(cls)) return el;
    return null;
  }
  contains(other) { for (let el = other; el; el = el.parent) if (el === this) return true; return false; }
  ancestorOrSelf(test) { for (let el = this; el; el = el.parent) if (test(el)) return true; return false; }
  isInert() { return this.ancestorOrSelf((el) => el.hasAttribute("inert")); }
  hiddenFromAT() { return this.ancestorOrSelf((el) => el.hasAttribute("inert") || el.getAttribute("aria-hidden") === "true" || el.hidden); }
  isFocusable() {
    const tabindex = this.getAttribute("tabindex");
    const interactive = NATIVELY_FOCUSABLE.has(this.tag) || (tabindex !== null && Number(tabindex) >= 0);
    const displayNone = this.ancestorOrSelf((el) => /display\s*:\s*none/.test(el.getAttribute("style") || ""));
    return interactive && !this.disabled && !this.ancestorOrSelf((el) => el.hidden) && !displayNone && !this.isInert();
  }
  // In the Tab sequence: focusable and not tabindex="-1".
  isTabbable() { return this.isFocusable() && this.getAttribute("tabindex") !== "-1"; }
  focus() { if (this.ownerDocument && this.isFocusable()) this.ownerDocument.activeElement = this; }
  // A browser moves focus to <body> when the focused element becomes inert or hidden.
  #fixFocus() {
    const doc = this.ownerDocument, active = doc && doc.activeElement;
    if (active instanceof FakeElement && !active.isFocusable()) doc.activeElement = doc.body;
  }
}

// The opening tag of the element with this id in the page's own markup (outside the script), as `{ tag, attrs }`:
// lets a test build its fake element from the real HTML, so a change to the markup changes the test's subject.
export function markupOf(id) {
  const html = readFileSync(INDEX_PATH, "utf8").replace(/<script>[\s\S]*?<\/script>/, "");
  const open = html.match(new RegExp(`<([a-z0-9]+)\\s[^>]*\\bid="${id}"[^>]*>`));
  if (!open) throw new Error(`no element with id="${id}" in the page markup`);
  const attrs = {};
  for (const m of open[0].matchAll(/\s([a-z][a-z0-9-]*)(?:="([^"]*)")?/g)) attrs[m[1]] = m[2] ?? "";
  return { tag: open[1], attrs };
}

// A FakeElement built from the real markup of `id`.
export function markupElement(id, overrides = {}) {
  const { tag, attrs } = markupOf(id);
  return new FakeElement(id, { tag, attrs, ...overrides });
}

// Objects built inside the vm context have foreign prototypes, which deepStrictEqual rejects: compare plain JSON.
export const plain = (value) => JSON.parse(JSON.stringify(value));
