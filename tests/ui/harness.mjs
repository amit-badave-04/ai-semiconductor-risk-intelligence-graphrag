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
  "evidenceView", "escalationStatus", "CITE",
];

function stubElement() {
  return {
    style: {}, dataset: {}, innerHTML: "", textContent: "", value: "", hidden: false, disabled: false,
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, appendChild() {}, setAttribute() {}, querySelectorAll() { return []; },
    closest() { return null; },
  };
}

export function pageScript() {
  const html = readFileSync(INDEX_PATH, "utf8");
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  if (scripts.length !== 1) throw new Error(`expected exactly one inline <script>, found ${scripts.length}`);
  // The server substitutes the Turnstile site key; an empty key keeps the widget code inert in tests.
  return scripts[0][1].replace("__TURNSTILE_SITE_KEY__", "");
}

export function loadPage() {
  const elements = new Map();
  const document = {
    getElementById(id) { if (!elements.has(id)) elements.set(id, stubElement()); return elements.get(id); },
    addEventListener() {}, createElement() { return stubElement(); }, head: { appendChild() {} },
  };
  // fetch never settles: the page's start-up calls (stats, examples) must not resolve or reject during a test.
  const sandbox = { document, window: {}, fetch: () => new Promise(() => {}), console, TextDecoder };
  const context = vm.createContext(sandbox);
  const api = vm.runInContext(`${pageScript()}\n;({ ${EXPORTS.join(", ")} })`, context);
  return { api, elements };
}

// Objects built inside the vm context have foreign prototypes, which deepStrictEqual rejects: compare plain JSON.
export const plain = (value) => JSON.parse(JSON.stringify(value));
