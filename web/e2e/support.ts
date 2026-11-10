import type { Locator, Page } from "@playwright/test";

/** What a page did while the test drove it (S5: zero violations, zero console errors, one origin, every request answered). */
export interface Watch {
  consoleErrors: string[];
  pageErrors: string[];
  foreignRequests: string[];
  failedResponses: string[];
  assetResponses: { url: string; status: number }[];
  /** `securitypolicyviolation` events seen by the page itself. */
  cspViolations: () => Promise<string[]>;
}

const CSP_RECORDER = `
  window.__cspViolations = [];
  document.addEventListener("securitypolicyviolation", (event) => {
    window.__cspViolations.push(event.violatedDirective + " " + event.blockedURI);
  });
`;

/** Start recording on a page BEFORE the first navigation. `allowedOrigins` are the page's own origin and, when the page may load
 *  it, https://challenges.cloudflare.com (nothing else is ever allowed). */
export async function watch(page: Page, ownOrigin: string, extraOrigins: string[] = []): Promise<Watch> {
  const allowed = new Set([ownOrigin, ...extraOrigins]);
  const w: Watch = {
    consoleErrors: [],
    pageErrors: [],
    foreignRequests: [],
    failedResponses: [],
    assetResponses: [],
    cspViolations: () => page.evaluate(() => (window as unknown as { __cspViolations?: string[] }).__cspViolations ?? []),
  };
  await page.addInitScript(CSP_RECORDER);
  page.on("console", (message) => {
    if (message.type() === "error") w.consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => w.pageErrors.push(error.message));
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.protocol === "data:" || url.protocol === "blob:" || url.protocol === "about:") return;
    if (!allowed.has(url.origin)) w.foreignRequests.push(request.url());
  });
  page.on("response", (response) => {
    const url = response.url();
    if (url.includes("/assets/")) w.assetResponses.push({ url, status: response.status() });
    if (response.status() >= 400) w.failedResponses.push(`${response.status()} ${url}`);
  });
  return w;
}

/** The first-class assertions of S5 and R12 as one list of problems (empty is a pass). */
export async function problems(w: Watch): Promise<string[]> {
  return [
    ...w.consoleErrors.map((m) => `console error: ${m}`),
    ...w.pageErrors.map((m) => `page error: ${m}`),
    ...w.foreignRequests.map((u) => `request to another origin: ${u}`),
    ...w.failedResponses.map((r) => `failed response: ${r}`),
    ...(await w.cspViolations()).map((v) => `CSP violation: ${v}`),
  ];
}

/** The origin the Turnstile script is allowed to come from (the CSP names it). */
export const CLOUDFLARE = "https://challenges.cloudflare.com";

/** What the stub exposes on `window.__turnstileStub` (a test reads and drives it with `page.evaluate`). */
export interface TurnstileStubState {
  renders: number;
  removes: number;
  resets: number;
  live: number;
  /** Play the visitor: hand the live widget's callback a fresh token and return it. */
  solve: () => string;
  expire: () => void;
}

// The script the Cloudflare address answers with. It is a fake `window.turnstile` (nothing here ever solves or bypasses a real
// challenge) that draws one marker element per live widget and calls the `onload` hook named in its own address, as Cloudflare's does.
const TURNSTILE_STUB_SCRIPT = `
(function () {
  var state = window.__turnstileStub = { renders: 0, removes: 0, resets: 0, issued: 0, widgets: {} };
  Object.defineProperty(state, "live", { get: function () { return Object.keys(state.widgets).length; } });
  function first() { return state.widgets[Object.keys(state.widgets)[0]]; }
  state.solve = function () { state.issued += 1; var token = "stub-token-" + state.issued; first().options.callback(token); return token; };
  state.expire = function () { first().options["expired-callback"](); };
  window.turnstile = {
    render: function (host, options) {
      state.renders += 1;
      var id = "stub-" + state.renders;
      var frame = document.createElement("div");
      frame.setAttribute("data-widget", id);
      host.appendChild(frame);
      state.widgets[id] = { host: host, frame: frame, options: options };
      return id;
    },
    remove: function (id) { state.removes += 1; if (state.widgets[id]) { state.widgets[id].frame.remove(); delete state.widgets[id]; } },
    reset: function (id) { if (state.widgets[id]) state.resets += 1; }
  };
  var hook = new URL(document.currentScript.src).searchParams.get("onload");
  if (hook && typeof window[hook] === "function") window[hook]();
})();
`;

/** Answer every request to the Cloudflare origin with the fake Turnstile script, at the network layer, so the page's CSP is
 *  exercised for real (the real widget is the owner's check, B6). Returns the URLs requested, in order. Call BEFORE navigating. */
export async function stubTurnstile(page: Page): Promise<string[]> {
  const requested: string[] = [];
  await page.route(`${CLOUDFLARE}/**`, async (route) => {
    requested.push(route.request().url());
    await route.fulfill({ status: 200, contentType: "text/javascript", body: TURNSTILE_STUB_SCRIPT });
  });
  return requested;
}

/** The API requests every page makes: the shell's GET /api/config (R8) and its one GET /api/freshness for the header line (R26). */
export const SHELL_API_PATHS: readonly string[] = ["/api/config", "/api/freshness"];

/** True for a request to the API that a PAGE made (not the shell's own /api/config and /api/freshness): the specs that say "this page asks the
 *  API for nothing" use it. */
export function isPageApiRequest(url: string): boolean {
  const { pathname } = new URL(url);
  return pathname.startsWith("/api/") && !SHELL_API_PATHS.includes(pathname);
}

/** The first starter question of the Ask page (docs/specs/m5b-redesign.md RD5: six curated examples, answered from the cache). */
export function firstStarter(page: Page): Locator {
  return page.getByRole("region", { name: /Or try an example/ }).getByRole("listitem").first().getByRole("button");
}

/** Choose a Depth in "More options" (RD7: Standard = hybrid, Deep research = agent, Basic text search = vector). */
export async function chooseDepth(page: Page, depth: "Standard (recommended)" | "Deep research" | "Basic text search"): Promise<void> {
  const toggle = page.getByRole("button", { name: /More options/ });
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  await page.getByRole("group", { name: "Depth" }).getByRole("radio", { name: depth }).check();
}
