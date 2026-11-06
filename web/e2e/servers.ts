// The two fixture servers the specs talk to (tests/e2e/serve_fixture_app.py: the real routers and the real ui_v2 over fakes,
// serving web/dist; no Neo4j, no LLM, no .env). PREVIEW is UI_V2_PREVIEW (the app at /v2/, the old page at /legacy);
// FLIPPED is UI_V2 (the same dist at /, /v2/ still an alias).
export const PREVIEW_PORT = 8765;
export const FLIPPED_PORT = 8766;
export const PREVIEW = `http://127.0.0.1:${PREVIEW_PORT}`;
export const FLIPPED = `http://127.0.0.1:${FLIPPED_PORT}`;

/** The five pages as served under each prefix, with the h1 each one shows. */
export const PAGES = [
  { path: "", heading: "Ask" },
  { path: "company/NVDA/", heading: "Nvidia (NVDA)" },
  { path: "workspace/", heading: "Workspace" },
  { path: "freshness/", heading: "Freshness" },
  { path: "method/", heading: "Method & Evidence" },
] as const;
