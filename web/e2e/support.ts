import type { Page } from "@playwright/test";

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
