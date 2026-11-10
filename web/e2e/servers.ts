// The fixture servers the specs talk to (tests/e2e/serve_fixture_app.py: the real routers and the real ui_v2 over fakes, serving
// web/dist; no Neo4j, no LLM, no .env). PREVIEW is UI_V2_PREVIEW (the app at /v2/, the old page at /legacy, the agent strategy
// on); FLIPPED is UI_V2 (the same dist at /, /v2/ still an alias); SITE_KEY is a preview server that hands out a dummy Turnstile
// site key and stubs the bot check (any non-empty token passes, none is a 403).
//
// Every port derives from ONE variable so two Playwright runs can go at once on different bases (E2E_PORT_BASE=8865 next to the
// default). Offsets: +0 preview, +1 flipped, +2 is left alone (web/scripts/lighthouse.mjs binds 8767 = the default base + 2),
// +3 site key.
const DEFAULT_PORT_BASE = 8765;

function portBase(): number {
  const raw = process.env.E2E_PORT_BASE;
  if (raw === undefined || raw === "") return DEFAULT_PORT_BASE;
  const base = Number(raw);
  if (!/^\d+$/.test(raw) || !Number.isInteger(base) || base < 1024 || base > 65000) {
    throw new Error(`E2E_PORT_BASE must be an integer from 1024 to 65000, got ${JSON.stringify(raw)}`);
  }
  return base;
}

const BASE = portBase();
export const PREVIEW_PORT = BASE;
export const FLIPPED_PORT = BASE + 1;
export const SITE_KEY_PORT = BASE + 3;
export const PREVIEW = `http://127.0.0.1:${PREVIEW_PORT}`;
export const FLIPPED = `http://127.0.0.1:${FLIPPED_PORT}`;
export const SITE_KEY = `http://127.0.0.1:${SITE_KEY_PORT}`;
/** The dummy key the site-key server answers /api/config with (not a Cloudflare key). */
export const DUMMY_SITE_KEY = "dummy-site-key";

/** The five pages as served under each prefix, with the h1 each one shows, the tab title, and the name of its link in the main navigation
 *  (docs/specs/m5b-redesign.md RD3; the Freshness link is the header's freshness line, which the fixture server's disabled
 *  monitor words "Data status unavailable"; the link name starts "Freshness: " for screen readers). */
export const NAV_FRESHNESS = "Freshness: Data status unavailable";
export const ASK_HEADING = "Ask about 13 chip and AI companies.";
export const PAGES = [
  // the Ask page's h1 is a sentence (docs/specs/m5b-redesign.md RD4); its tab is named "Ask"
  { path: "", heading: ASK_HEADING, title: "Ask", nav: "Ask" },
  { path: "company/NVDA/", heading: "Nvidia (NVDA)", title: "Nvidia (NVDA)", nav: "Companies" },
  { path: "workspace/", heading: "Workspace", title: "Workspace", nav: "Workspace" },
  { path: "freshness/", heading: "Freshness", title: "Freshness", nav: NAV_FRESHNESS },
  { path: "method/", heading: "Method & Evidence", title: "Method & Evidence", nav: "How it works" },
] as const;
