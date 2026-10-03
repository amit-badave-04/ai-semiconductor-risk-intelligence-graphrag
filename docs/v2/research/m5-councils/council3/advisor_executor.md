**Hosting.** H1 is the only option with a Monday-morning first step: add a Node build stage to the Dockerfile, mount `StaticFiles` after the API routes, publish at `/v2/`. Proof it worked: a CI smoke test against the built image requests every route with its trailing slash (`/company/NVDA/`) and gets 200, while `/api/*` still returns JSON. H2 and H3 each need a new vendor, hostname or payment detail before step one, and the brief lists their costs: a 100k/day Worker cap, CORS and third-party-cookie breakage, a 125 s proxy timeout, and H3 ends open public access. Defer all of Cloudflare to M6.

**Framework.** F3 stops being workable once the dossier, workspace, freshness, map and admin views go into a 1,040-line file with inline script and a strict CSP. F1 adds work the brief shows is not frontend-only: a new `/api/chat` encoder, byte-level fixtures, and a major-version churn about twice a year. It also adds an AI SDK whose renderer we bypass anyway, since we write our own restricted renderer. F2 reuses the existing SSE grammar and needs no backend change. I recommend F2. This deviates from the owner's recorded choice, so it is the owner's call, and the evidence is the encoder requirement and the unused renderer. ASSUMPTION: if the owner insists on Next.js, a static export with the thin SSE client (no AI SDK) is the fallback.

**Rollout.** Ship at an unlinked `noindex` `/v2/`, keep `/legacy`. Promote only on written sign-off, after these pass: the 149 node tests ported or green against the new bundle, the CSP and `textContent` pins, the chip-grammar diff test, a Turnstile token per ask and per upload, the `sessionStorage`-only workspace, both known a11y gaps fixed, and Lighthouse accessibility >= 95.

**M5c.** Ship export and the one-turn follow-up first. Keep the MCP server and API keys gated and off in production until keys and quotas are tested. Defer the admin console and the supply-chain map. Omit the dropped-risk badge.

**Biggest risk.** Static-export routing and CSP regressions under FastAPI. Detect it with the Docker smoke test in week one.

**Choices:** H1, F2 (owner's call), gated exposure for the MCP server and API keys.
