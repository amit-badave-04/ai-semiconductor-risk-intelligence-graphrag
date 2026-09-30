# M5 frontend stack — re-verification as of 2026-09-30

Scope: re-verify the owner's 2026-09-25 decision ("Next.js static + Vercel AI SDK on Cloudflare
Pages", backend FastAPI on Fly.io streaming SSE, Turnstile already in use) against live sources.
Baseline: `docs/v2/research/research-scale-frontend.md` §B "Frontend" (lines 137-186), folded into
`docs/v2/PLAN.md:12,40,103` and `docs/v2/REVIEW_2026-09-26.md:70,72,77,88,124` — note
`REVIEW_2026-09-26.md:77` records a **separate, named owner ask** (R15): "$30 budget,
cheap+escalate, US-hosted open weights, **Next.js + AI SDK**". This matters for the recommendation
in §9: the Next.js choice is not only the prior research doc's inference, it is also something the
owner asked for directly, elsewhere.

Every claim below is tagged **VERIFIED** (seen directly on a primary source — npm/PyPI registry
JSON, a vendor doc page, a GitHub API response, or repo code) or **UNVERIFIED** (secondary source,
search-engine synthesis, or inference). All web sources accessed 2026-09-30.

## 0. Repo facts that constrain the decision (read-only, this session)

- **No CORS anywhere in `src/`.** `grep -rn "CORSMiddleware|allow_origins|CORS" src/` → no matches.
  The app is created at `src/semigraph/serve/main.py:265` (`FastAPI(title="semigraph", ...)`) with
  no `CORSMiddleware` added. Today the UI is same-origin (`static/index.html` served by the same
  FastAPI app), confirmed by `index.html:665,734,969,971,997,1013,1021` all calling relative
  `fetch("/api/...")`. `wsHeaders()` (`index.html:789`, `const wsHeaders = () => (currentWorkspace ?
  { "X-Workspace-Token": currentWorkspace.token } : {})`) and `X-Turnstile-Token`
  (`index.html:969`) are custom, non-simple headers — on a cross-origin call these force a CORS
  preflight (`OPTIONS`) that FastAPI's `CORSMiddleware` must answer explicitly
  (`allow_methods`, `allow_headers`).
- **No Cloudflare edge in front of the live deployment today — but a generic trusted-header hook
  already exists, pointed at Fly, not Cloudflare.** `config.py:70`:
  `client_ip_header: str = ""` — "header set by a TRUSTED proxy (Fly: fly-client-ip); empty = socket
  address" — disabled by default. `guard.py:57-62`'s `client_ip()` only honors a forwarding header
  "ONLY when the deployment names it (`CLIENT_IP_HEADER=fly-client-ip` on Fly...); any other header
  is attacker controlled and would turn the per-address limiter into a no-op." **`origin-auth` and
  `CF-Connecting-IP` specifically do not appear anywhere in `src/`** (targeted greps for those exact
  strings, case-insensitive, returned nothing) — that half of `PLAN.md:40,103`'s edge is genuinely
  unbuilt. `docs/v2/M1_REPORT.md:72` names the live URL as **`https://semigraph.fly.dev`** directly
  (bare Fly domain, not a custom Cloudflare-zone domain); its "Cloudflare 'Verify you are human'
  challenge" is inferred here to mean the in-page Turnstile widget the app embeds, not a Cloudflare
  reverse-proxy/WAF in front of `fly.dev` — inference, not a direct statement in that doc, but a bare
  `fly.dev` host cannot sit behind the reporter's own Cloudflare zone.
  **Consequence for §5**: whichever path is chosen (Worker proxy or a Cloudflare-zone `api.<domain>`
  in front of Fly), Fly will see the proxy's or Worker's IP, not the browser's, unless
  `CLIENT_IP_HEADER` is repointed at the new edge's real header (`cf-connecting-ip` for a Cloudflare
  proxy) — the mechanism to do this already exists and is a one-line config change, not new code.
  The per-address rate limiter and Turnstile's `remoteip` both depend on getting this right, and the
  bare `fly.dev` origin stays directly reachable (bypassing whatever edge is added) unless Fly-side
  config blocks non-edge traffic — both are real M5 work items, not solved by the frontend choice.
- **`verify_turnstile` does not check the response's `hostname` field**
  (`src/semigraph/serve/guard.py:130-155`): it posts `secret`, `response`, `remoteip` and only reads
  `success`. Hostname enforcement today lives entirely in the Cloudflare-side sitekey's allowed-hostname
  list, not in this code.
- **No `package.json`, no npm deps, no Playwright anywhere in the repo.** `tests/ui/harness.mjs:1-9`
  runs the 1,042-line `static/index.html`'s inline `<script>` in a Node `vm` context with
  `node --test`, "No DOM library, no npm packages". A real component framework (Next or Vite) is a
  green-field addition, not a migration of an existing toolchain.
- **`pyproject.toml:32`**: `fastapi>=0.141` — current, no action needed.
- **v1's sync generator can't cancel LLM calls on client disconnect** (per the 2026-09-25 doc,
  `research-scale-frontend.md:27`: `routes.py:164-201`'s `_paid_stream` is a sync generator holding
  a threadpool thread). Whatever frontend ships, an aborted stream (`useChat`'s `stop()`, a tab
  close) only closes the HTTP connection on the client side — unless the M5 async-serving rewrite
  (already in scope, `PLAN.md:103`) detects the disconnect and cancels the in-flight LLM call
  server-side, an aborted answer keeps consuming paid tokens after the user walks away.
- **M5 scope is large** (`docs/v2/PLAN.md:103`): Home, Answer (step timeline, `[n]` citation chips →
  drawer, freshness badges), dossier + what-changed, supply-chain map (React Flow + table toggle),
  PDF/Markdown export with footnoted citations, Freshness page, Method/Limits page, conversation
  history, OpenAPI, MCP server, Access-gated admin console. A genuine multi-screen app.

## Delta vs. the 2026-09-25 doc

| # | Prior claim | Verdict 2026-09-30 |
|---|---|---|
| 1 | "`ai@7.0.114` is Apache-2.0" | **CONFIRMED** (now 7.0.123, same day, published today). VERIFIED. |
| 2 | "Majors 5, 6 and 7 all shipped on 2026-09-24" | **WRONG** — an artifact of checking `dist-tags`/latest-patch on one day, not three majors born together. Real major dates (npm `time`, VERIFIED): **5.0.0 → 2025-07-31, 6.0.0 → 2025-12-22 (~5 mo later), 7.0.0 → 2026-06-25 (~6 mo later)**. Real cadence ≈ 2 majors/year. |
| 3 | "AI Elements needs Next.js" (`research-scale-frontend.md:148` ties this specifically to AI Elements) | **WRONG.** `packages/elements/package.json` (VERIFIED, GitHub API, `vercel/ai-elements`) depends only on `react`, `ai`, `@xyflow/react`, `streamdown`, `@repo/shadcn-ui`, radix — **zero `next` dependency**. `gh search code "next/" repo:vercel/ai-elements` outside `apps/` (the Next-based *docs site*) returns only a regex literal (`/next/i`) in a test file, not an import. AI Elements is React + Tailwind + shadcn and installs into a Vite app via the shadcn CLI (§3). |
| 4 | "Skip `react-force-graph` (last release Feb 2026)" | **STALE claim.** npm shows `react-force-graph@1.48.3` published **2026-09-29**, one day before this report. Not abandoned — re-judge on architecture fit (Three.js/D3-force, heavier runtime, different mental model than React Flow), not recency. |
| 5 | "Hand-write ~100-line Python encoder; no maintained one exists" | **Still the right call, more thoroughly checked.** `pydantic-ai` (MIT, v2.52.0, published **2026-09-30**) ships `pydantic_ai.ui.vercel_ai.VercelAIAdapter`, but its base `UIAdapter` (VERIFIED, GitHub source) is hard-typed to `agent: AbstractAgent` and calls `agent.run_stream_events()` — coupled to pydantic-ai's own agent loop, not a generic encoder; cannot take a LangGraph `astream()` without rewriting the agent layer in pydantic-ai. Three further Python candidates were checked and all fall short: `keurcien/langchain-ai-sdk-adapter` (`langchain_ai_sdk_adapter` on GitHub, VERIFIED by reading its README — explicitly scoped to "Agent Frameworks: LangGraph" + "API Frameworks: FastAPI", i.e. this project's exact stack) implements the **older "Data Stream Protocol"**, not the current `x-vercel-ai-ui-message-stream` protocol (VERIFIED, its own README names "Data Stream Protocol" and never mentions the current header) — and has 0 GitHub stars, last updated 2026-03-18 (VERIFIED), i.e. an unmaintained personal project on the wrong protocol version; `elementary-data/py-ai-datastream` (Apache-2.0, 28 stars, last push 2026-04-27) is the same situation — VERIFIED via its own README as implementing "the Data Stream Protocol," not the UI message stream protocol; `vercel-ai-sdk` on PyPI is `0.0.1.dev10`, pre-alpha. **Net: still hand-write the encoder** against LangGraph's `stream_mode=["messages","custom","updates"]`, optionally schema-checked against `pydantic_ai.ui.vercel_ai.response_types`'s Pydantic models for the part shapes; `vercel-labs/ai-sdk-preview-python-streaming` (no license, last push 2026-04-19) is usable only as an unlicensed wire-format reference, not as reusable code. |
| 6 | Cloudflare Pages recommendation, no mention of Workers | **Materially outdated.** Cloudflare's own Pages docs now carry the banner (VERIFIED, `developers.cloudflare.com/pages/`, fetched directly): *"Workers supports most Pages use cases and offers a broader feature set. It is Cloudflare's primary platform for building applications. **Start new projects with Workers.**"* See §5. |
| 7 | "@xyflow/react 12.12.0", "sigma 3.0.3", "cytoscape 3.34.3" | **CONFIRMED**, all MIT, all actively maintained (xyflow: release + commit on 2026-09-24, six days before this report; cytoscape published 2026-09-07; sigma 2026-04-30). |

## 1. Next.js current stable + static export

- **Current stable: 16.3.7** (VERIFIED, npm dist-tags.latest). MIT license. Major cadence is
  annual in October: 13.0.0 (2022-10-25), 14.0.0 (2023-10-26), 15.0.0 (2024-10-21), **16.0.0
  (2025-10-22)** — all VERIFIED. 16.x is ~11 months into its cycle; a 17.0.0 within weeks is
  plausible on this cadence but unannounced (UNVERIFIED speculation, flagged as such).
- **React version**: Next 16.3.7's own `peerDependencies` (VERIFIED, `npm view next@16.3.7
  peerDependencies`) accept `^18.2.0 || 19.0.0-rc-... || ^19.0.0` — React 19 is not strictly
  required, though `create-next-app` defaults to it. Current npm latest `react`/`react-dom` is
  **19.3.0**, published 2026-09-09 (VERIFIED).
- **`output: 'export'` constraints** (VERIFIED, `nextjs.org/docs/app/guides/static-exports`,
  page banner `version: 16.3.7`, `lastUpdated: 2026-08-25`):
  - **Unsupported**: Server Actions, Route Handlers that read the incoming `Request`, `cookies()`,
    `rewrites`/`redirects`/`headers` config, `proxy` (middleware), Dynamic Routes with
    `dynamicParams: true` or without `generateStaticParams()`, ISR, Draft Mode, Intercepting Routes.
    Attempting any of these under static export "will result in an error" at build/dev time — a real
    footgun cost for a one-developer team (see §9).
  - **Route Handlers still work** but only `GET`, and only when explicitly marked
    `export const dynamic = 'force-static'` — they bake to a static file at build time, so they are
    useless for this project's live `/api/*` (which stays on FastAPI regardless of frontend choice).
  - **`next/image`** needs a custom `loader` (no built-in optimizer) — not needed here.
  - **Dynamic routes**: M5's ticker pages (a fixed ~10-company corpus) fit `generateStaticParams()`
    cleanly. **Workspace pages cannot** — workspace IDs are created at runtime by
    `POST /api/workspace` (`index.html:997`), so they must be client-routed
    (e.g. `/workspace?id=...`), never pre-generated. State this explicitly in the M5 plan.

## 2. Vercel AI SDK

- **Current major: 7** (`ai@7.0.123`, Apache-2.0, published 2026-09-30 — VERIFIED).
- **`ai` ↔ `@ai-sdk/react` major mapping** (VERIFIED, npm `time` for both packages): they ship
  their majors on the *same day*, offset by exactly 3: `ai 4.0.0` / `@ai-sdk/react 1.0.0` both
  2024-11-18; `ai 5.0.0` / `@ai-sdk/react 2.0.0` both 2025-07-31; `ai 6.0.0` / `@ai-sdk/react 3.0.0`
  both 2025-12-22; `ai 7.0.0` / `@ai-sdk/react 4.0.0` both 2026-06-25. So **`@ai-sdk/react` N pairs
  with `ai` (N+3)**.
- **AI Elements is pinned to the v6 generation, not v7.** Its `package.json` (last repo push
  2026-09-01) declares `"ai": "^6.0.105"` and (devDependency) `"@ai-sdk/react": "^3.0.41"` — the
  matched v6 pair. `ai@6.0.297` was published **today** (2026-09-30, VERIFIED release list), so the
  v6 line is still receiving patches, not dead. A `gh search issues/prs "v7" repo:vercel/ai-elements`
  found no open discussion of v7 support (one closed, unrelated attachments bug). **Practical
  pinning advice: pin the whole `ai`/`@ai-sdk/react` pair that AI Elements' own `package.json`
  declares (currently the v6 line) rather than chasing `ai@7`, until AI Elements itself bumps.**
- **Protocol header** (VERIFIED, `ai-sdk.dev/docs/ai-sdk-ui/stream-protocol`): response header
  **`x-vercel-ai-ui-message-stream: v1`**, SSE framing (`Content-Type: text/event-stream`). The
  **stream terminates with a literal `data: [DONE]` line** (VERIFIED, same page) — a hand-written
  Python encoder must emit this exact closing line, not just close the connection. Nothing on the
  fetched docs indicated this header/termination changed across v5/v6/v7; the separate versioning
  guide (`ai-sdk.dev/docs/migration-guides/versioning`) says nothing about protocol-version bumps
  between majors — treat the wire format as stable, but keep a contract test regardless.
- **Part types** (VERIFIED, same fetch): message control (`start`/`finish`/`abort`), text
  (`text-start`/`text-delta`/`text-end`), reasoning (`reasoning-start/-delta/-end`,
  `reasoning-file`), tool parts (input `start/delta/available`, `tool-output-available`,
  `tool-output-denied`, approval `request`/`response`), step management
  (`start-step`/`finish-step`/`reset-step`), plus `error`, `source-url`, `source-document`, `file`,
  `data-*`.
- **Rendering tool/step/custom-data parts** (VERIFIED, `ai-sdk.dev/docs/ai-sdk-ui/streaming-data`):
  the client filters `message.parts` by `part.type` (e.g. `part.type === 'data-weather'`) and
  renders per type; `start-step`/`finish-step` bound a step's parts (M5's "Searched 3 SEC sources"
  timeline). A data part written with `transient: true` is **not** added to `message.parts`/history
  and is only observable via the `onData` callback — good for ephemeral progress pings.
- **`useChat` + `DefaultChatTransport` cross-origin** (VERIFIED, `ai-sdk.dev/docs/ai-sdk-ui/transport`):
  `credentials` accepts a static value or a function; `headers` accepts a function re-evaluated per
  request (`headers: () => ({...})`) — exactly the mechanism needed for a **fresh** Turnstile token
  on every send, matching `index.html:762-777`'s `waitForFreshTurnstileToken`/`resetTurnstile`
  pattern. `prepareSendMessagesRequest` also exists to reshape headers+body per call. The docs did
  not explicitly confirm an absolute cross-origin `api` URL in the excerpt fetched (UNVERIFIED by
  direct quote, though transports are plain `fetch` wrappers built around exactly this case) —
  smoke-test before relying on it for the demo.
- **Resume/abort** (VERIFIED, `ai-sdk.dev/docs/ai-sdk-ui/chatbot-resume-streams`): resumable
  streaming **requires server-side stream persistence** (reference impl uses Redis via
  `resumable-stream`, ISC, v2.2.13, published 2026-09-16 — VERIFIED) plus a
  `GET /api/chat/[id]/stream` reconnect endpoint (204 if nothing active). Critically: **a client
  `stop()`/reload is just a disconnect, not a cancel** — generation keeps running server-side unless
  a dedicated stop endpoint persists the partial answer and actually cancels the work (ties directly
  into the v1 sync-generator problem in §0). For M5: either skip true resume (acceptable — v1 has no
  checkpointer, `research-scale-frontend.md:61`) and support only client-side abort, or add a small
  Valkey-backed stream registry — real backend work either way.
- **Node.js requirement** (VERIFIED, `ai-sdk.dev/docs/migration-guides/migration-guide-7-0`, fetched
  directly): *"AI SDK 7.0 requires Node.js 22 or later. The SDK is tested on Node.js 22, 24, and
  26."* Also ESM-only (no CommonJS), `system` renamed to `instructions`, `onFinish`→`onEnd` and
  `onStepFinish`→`onStepEnd`, OpenTelemetry moved to a separate `@ai-sdk/otel` package. This is a
  constraint on whatever build image runs `next build`/`vite build` in CI, independent of the
  FastAPI backend's Python runtime — but moot for M5 in practice since AI Elements itself is still
  pinned to the v6 line (below), where Node 22 is not mandated as strictly.

## 3. AI Elements and shadcn/ui

- **AI Elements**: Apache-2.0 (`LICENSE` file, `vercel/ai-elements`, VERIFIED), 2,468 stars, last
  push 2026-09-01 (VERIFIED, `pushed_at`) — active, not daily-churn. **Does not require Next.js**
  (delta row 3) — only `react`, `ai`/`@ai-sdk/react` (v6 generation, see §2), `@xyflow/react`,
  `streamdown` (markdown/code/math), radix primitives, plus a Tailwind + shadcn `components.json`.
- **shadcn/ui + Vite**: officially first-class (VERIFIED, `ui.shadcn.com/docs/installation/vite`,
  "Install and configure shadcn/ui for Vite" — a top-level guide alongside Next.js, not a
  workaround). shadcn is copy-in component source, not a runtime dependency, so it carries no
  framework lock-in either way.
- Net effect: **the strongest technical argument for Next.js does not hold** — but see §9 for why
  this alone doesn't settle the decision (R15 is a separate, named owner ask).

## 4. Graph visualization

- **`@xyflow/react` (React Flow) 12.12.0**, MIT, VERIFIED. The monorepo (`@xyflow/system`,
  `@xyflow/svelte`, `@xyflow/react`) had both a release and a commit dated **2026-09-24** — six days
  before this report, i.e. actively maintained, not "today." AI Elements itself depends on
  `@xyflow/react`, so adopting AI Elements brings React Flow along for free.
- **Performance at ~500–2,000 nodes**: React Flow's own performance page
  (`reactflow.dev/learn/advanced-use/performance`, fetched directly) gives **no concrete node-count
  thresholds or virtualization numbers** — only qualitative advice: memoize custom node/edge
  components (`React.memo`, `useCallback`/`useMemo` for props), avoid subscribing to the whole
  `nodes` array (causes re-renders on any change), collapse/hide nested nodes via the `hidden` prop,
  and keep node CSS simple (no heavy shadows/gradients) at scale. **No official answer for the
  500–2,000 range exists on React Flow's own docs**; broader community writeups (search-aggregated,
  UNVERIFIED) converge on "hundreds are comfortable with memoization, low thousands need care or a
  canvas/WebGL renderer instead." This report did not have a real node/edge count for this project's
  supply-chain graph to check against that range — get one from the actual corpus before committing,
  and prototype at the real scale rather than trusting either doc's guidance blindly.
- **Cytoscape 3.34.3**, MIT, VERIFIED, published 2026-09-07 — canvas-based, the natural fallback if
  node count climbs past DOM-rendering comfort.
- **Sigma 3.0.3**, MIT, VERIFIED, published 2026-04-30 — WebGL-based, for thousands+ nodes; pairs
  with `graphology`, whose **latest (`0.26.0`) was published 2025-01-26** (VERIFIED npm) — over a
  year with no release. Not necessarily abandoned, but confirm current upstream activity before
  depending on it beyond the pinned version.
- **`react-force-graph`**: not stale (delta row 4), but a materially different rendering model
  (Three.js/D3-force) — a fit only if a literal force-directed layout is wanted; a tiered
  supply-chain layout fits React Flow + dagre better regardless of freshness.
- **Verdict**: React Flow remains the reasonable default, reinforced by "bundled with AI Elements"
  if that library is adopted — but validate against the real graph size before finalizing.

## 5. Cloudflare Pages vs. Workers Static Assets in 2026

- **Cloudflare's own banner** (VERIFIED, `developers.cloudflare.com/pages/`, fetched directly):
  *"Workers supports most Pages use cases and offers a broader feature set. It is Cloudflare's
  primary platform for building applications. Start new projects with Workers."* Pages is not
  declared deprecated (the migrate-from-Pages guide, VERIFIED fetch, makes no deprecation claim and
  says existing Pages projects keep working), but all new-project guidance points at Workers.
- **`run_worker_first`** (VERIFIED, `developers.cloudflare.com/workers/static-assets/`): route
  patterns like `/api/*` invoke the Worker script before falling back to static assets — the
  mechanism for same-origin proxying to Fly, avoiding CORS entirely.
- **Workers Free plan limits** (VERIFIED, `developers.cloudflare.com/workers/platform/limits/`):
  **100,000 requests/day**, **10 ms CPU time per HTTP request** (wall-clock is unlimited while the
  client stays connected — a proxying Worker mostly awaits I/O, which doesn't count against CPU
  time, so a 10 ms cap is not necessarily fatal for SSE passthrough, but validate empirically), up to
  30 s extra via `ctx.waitUntil()`, **50 subrequests per request**. Static Assets: **20,000 files per
  Worker version**, **25 MiB per file** (VERIFIED same page; no aggregate total-size cap stated).
- **Billing** (VERIFIED, `.../billing-and-limitations/`): static asset requests are free and
  unlimited; **`run_worker_first`-matched requests invoke the Worker and count against the (Workers)
  quota** — every proxied `/api/*` call consumes one of the 100k/day free requests, not a free
  static-asset hit. Exceeding it returns 429, no silent fallback.
- **Custom domain asymmetry** (VERIFIED, Pages→Workers migration guide): **Pages supports custom
  domains outside a Cloudflare zone; Workers does not** — a Workers custom domain must live on a
  zone Cloudflare manages. This matters if `api.<domain>` (or the frontend's own domain) is
  registered/managed anywhere other than Cloudflare.
- **Cloudflare Access + cross-origin is a real complication, not a detail** (VERIFIED,
  `developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/cors/`):
  the `CF-Authorization` cookie is blocked as third-party on cross-site requests, and — more
  sharply — **a preflighted cross-origin request to an Access-protected origin gets a 403 on the
  OPTIONS itself**, because browsers never send cookies with `OPTIONS` and Access has nothing to
  authorize. Cloudflare's documented fixes are: bypass OPTIONS to origin, have Cloudflare answer the
  preflight itself with matching CORS headers, or route through a Worker that attaches a service
  token. **`PLAN.md:103`'s Cloudflare Access invite-gate for the admin console, combined with a
  `pages.dev`/`workers.dev` frontend calling a separate Access-protected `api.<domain>`, walks
  straight into this** if not designed around from the start.
- **Why not Vercel** (VERIFIED, `vercel.com/docs/plans/hobby`, `last_updated: 2026-09-14`): *"the
  Hobby plan restricts users to non-commercial, personal use only"* — this is the reason Vercel
  itself is out for a buyer-facing product, independent of the Pages/Workers question.
- **125 s proxy read timeout** (carried over from the 2026-09-25 doc as still relevant, not
  re-verified this session): if traffic routes through Cloudflare's proxy at all (custom domain,
  Access, or a Worker), SSE heartbeats remain necessary regardless of Pages/Workers/Vite/Next choice.

**What "Cloudflare Access invite gate" actually scopes**: `PLAN.md:103` lists "Cloudflare Access
invite gate + roles + tamper-evident audit log" as its own clause, syntactically separate from the
later "admin console (cost/answer, routing, eval scores, Langfuse link)" clause in the same
sentence. Read at face value, **Access gates the product itself (an invited-buyer demo with roles)**,
not only an internal admin panel — the admin console is a further, narrower surface inside that.
This reading was not independently confirmed elsewhere in the docs (UNVERIFIED beyond this
sentence's own structure), but it is the more natural parse and changes which path is the default.

**Revised recommendation for §5** (this is now genuinely two credible paths, not one clear winner —
flagging per the owner's llm-council criteria for "architectural lock-in," without running the
council here since this is a read-only research task, not the decision itself):

- **Path A — same-origin via Worker proxy (default, given Access likely gates the whole product).**
  Deploy the static frontend as Workers + Static Assets on one hostname, with
  `run_worker_first: ["/api/*"]` proxying to the Fly origin, and put Cloudflare Access on that same
  hostname. Everything the browser sees is same-origin, so there is no preflight and the
  `CF_Authorization` cookie is first-party — the Access+CORS preflight-403 problem in this section
  simply does not arise. Cost: the 100k/day free-request accounting now covers every `/api/*` call
  (§5 limits above), and the SSE-passthrough behavior through `run_worker_first` should be validated
  empirically (docs confirm no wall-clock limit while connected, but do not call out streaming
  specifically).
- **Path B — separate origins with real CORS.** Put a Cloudflare zone in front of the Fly app on a
  subdomain (`api.<domain>`, proxied) and add `CORSMiddleware` to FastAPI (§0). Only viable if the
  reading above is wrong and Access truly gates just the admin console: the buyer-facing
  `/api/ask` must then **not** sit behind Access, or every chat turn (which carries
  `X-Turnstile-Token`, a preflight-triggering header) hits the documented OPTIONS-403 problem.
  If this path is chosen anyway with Access covering the whole app, it needs Cloudflare's documented
  bypass-OPTIONS-to-origin setting *and* the frontend and API to share a registrable domain (a
  `pages.dev`/`workers.dev` origin calling `api.<domain>` is cross-site, and the `CF_Authorization`
  cookie is blocked as third-party in that case regardless of the OPTIONS fix).
- Both paths need a domain on a Cloudflare zone for Access + a custom API/app hostname; Path A can
  fall back to a bare `*.workers.dev` hostname if Access is dropped from M5, Path B cannot avoid a
  zone once Access is in scope at all.

## 6. Turnstile in a React SPA

- **No official Cloudflare-published React package exists.** VERIFIED directly against the npm
  registry search API for `scope:cloudflare turnstile` and related terms: the only Cloudflare-scoped
  hit is the unrelated `@cloudflare/turnstile-firebase-app-check`. Best community options:
  **`@marsidev/react-turnstile`** (MIT, v1.6.1, published 2026-08-26 — VERIFIED, actively
  maintained) and **`react-turnstile`** (MIT, v1.1.5, published 2026-01-30 — VERIFIED, slower
  cadence). Either wraps the same `challenges.cloudflare.com/turnstile/v0/api.js` script
  `index.html:776-777` already loads directly — a hand-rolled hook (as today) is equally valid.
- **Token refresh**: the existing pattern (`waitForFreshTurnstileToken`/`resetTurnstile`,
  `index.html:754-777` — render once, get a fresh single-use token per submit via the widget's
  `callback`, reset after each use) should carry over unchanged and maps directly onto AI SDK's
  per-request `headers: () => {...}` transport hook (§2).
- **Hostname allowlisting is about the frontend, not the API.** A Turnstile sitekey's allowed-hostname
  list must cover the hostname(s) where the *widget renders* (the frontend origin) — the API's
  origin is irrelevant to Turnstile regardless of same-origin (Path A) or cross-origin (Path B) API
  calls (§5).
- **Preview-deployment hostnames**: for Cloudflare Pages, per-deployment preview subdomains are
  commonly reported as auto-covered by the sitekey's hostname match (UNVERIFIED — secondary sources
  only, not confirmed on Cloudflare's own Turnstile docs in this session). For Workers, preview/
  version URLs follow `<version-prefix>-<worker-name>.<subdomain>.workers.dev`
  (VERIFIED, `developers.cloudflare.com/workers/configuration/previews/`) — a **new, unpredictable
  subdomain per version** — and that page says nothing about Turnstile at all (checked directly,
  UNVERIFIED that it's auto-covered). If Path A (§5, Workers) is chosen, verify preview-URL coverage
  empirically or fall back to Cloudflare's published test sitekeys for non-production builds — a
  standard, documented workaround either way.
- Recall from §0: `guard.py:130-155` never inspects the siteverify response's `hostname` field —
  hostname scoping today is entirely a Cloudflare-dashboard concern, not a backend check, and stays
  that way regardless of frontend framework.

## 7. Client-side PDF/Markdown export with footnoted citations

- **`jspdf` 4.2.1**, MIT (VERIFIED, npm + GitHub `parallax/jsPDF`, pushed 2026-09-30). Low-level
  canvas-style API; footnotes/citation numbering are hand-laid-out text positioning.
- **`pdfmake` 0.3.11**, **MIT** (VERIFIED directly from the `LICENSE` file and `package.json` in
  `bpampuch/pdfmake` — GitHub's license detector shows `NOASSERTION` only because of an unusual
  copyright-holder split across years; the license text itself is plain MIT). Declarative
  document-definition API, a more natural fit for a structured report with numbered footnotes than
  jsPDF's imperative calls. Last push 2026-06-12 (VERIFIED) — healthy, not fast-moving.
- **`@react-pdf/renderer` 4.9.0**, MIT (VERIFIED), last publish 2026-08-27 — React-component-based
  PDF authoring; best fit if the export view is built as React components mirroring the on-screen
  citation drawer.
- **Markdown export**: `react-markdown` (MIT, 10.1.0, last publish 2025-03-07 — VERIFIED, stable,
  no majors in over a year) + `remark-gfm` (MIT, 4.0.1, last publish 2025-02-10) cover on-screen
  rendering; the export path itself needs no library — serialize the already-normalized answer +
  `[n]` citation list straight to a `.md` string client-side.
- **None of these libraries has a built-in "footnote" primitive.** Build the export as data (answer
  text + an ordered evidence list, the same shape the drawer already renders from
  `/api/evidence/{chunk_id}`, `research-scale-frontend.md:147`) and feed it to whichever renderer,
  laying out numbered references and a "Sources" section as plain paragraphs. Pick by
  authoring-model fit: `@react-pdf/renderer` if the export UI is componentized, `pdfmake` if
  generated from a plain JS object.

## 8. Accessibility tooling for a static export

- **`axe-core` 4.13.0**, **MPL-2.0** (not MIT — flagging in case the prior doc implied otherwise),
  VERIFIED, last publish 2026-08-05. **`@axe-core/playwright` 4.13.0**, same license, published
  2026-08-11.
- **`tests/ui/` has no Playwright today** (§0) — adding `@axe-core/playwright` means introducing
  Playwright + a `package.json` for the first time, a real toolchain addition either way.
- **`@lhci/cli` (Lighthouse CI)**: 0.15.1, Apache-2.0, VERIFIED, but **last npm publish and last
  GitHub commit are both 2025-06-26** (VERIFIED) — over 15 months stale as of this report. Issues
  are still being filed and updated as recently as 2026-09-30 (VERIFIED), including at least one
  **open, unmerged fix PR** ("fix(utils): respect median-run aggregation in category assertions") —
  i.e. known bugs have proposed fixes sitting unmerged, not shipped. **Assessment: stalled release
  train, not confirmed abandoned, but do not rely on a near-term fix landing.** It still works
  against a static `out`/`dist` folder (`lhci autorun --collect.staticDistDir=...`) for either a
  Next export or a Vite build. **Alternative**: the plain **`lighthouse` CLI** (Apache-2.0, v13.5.0,
  published 2026-09-18 — much more active, VERIFIED) run directly against the built static files,
  paired with a simple threshold-check script instead of the stalled LHCI server.
- Both tools operate on rendered HTML/DOM — **this axis does not discriminate between Next static
  export and a Vite SPA build.**

## 9. The real decision: Next.js static export vs. Vite + React SPA vs. extending the current page

**This is a genuine fork with an owner ask on one side, not a clean-cut technical winner — flagging
rather than resolving it unilaterally.** `REVIEW_2026-09-26.md:77` records R15 as an explicit owner
ask: "$30 budget, cheap+escalate, US-hosted open weights, **Next.js + AI SDK**." The
2026-09-25 research doc's argument for Next.js rested partly on "AI Elements needs Next.js"
(`research-scale-frontend.md:148`), which §3 shows is false — but R15 is independent of that
argument and still stands as something the owner asked for directly.

| Factor | Next.js (`output: 'export'`) | Vite + React SPA |
|---|---|---|
| Owner ask (R15) | **Matches directly** | Departs from the recorded ask |
| Dynamic workspace routes | Must be client-routed anyway (§1) — `generateStaticParams` buys nothing for them | Same client-routing, one less concept |
| Prerendered HTML for Home/Method/Freshness | Real SSG win — crawlable, fast first paint | SPA shell only; likely irrelevant behind Cloudflare Access for a buyer demo, matters more for a future public marketing surface |
| Static-export footguns | Touching an unsupported feature (Server Actions, middleware, cookies, etc.) "will result in an error" (§1) — a real debugging tax for one developer | No such trap; plain SPA + React Router |
| Build toolchain size | App Router conventions + export-mode edge cases | Minimal, well-understood Vite + React |
| SSE/AI SDK fit | Identical — `useChat`/`DefaultChatTransport` doesn't care which bundler served the page | Identical |
| AI Elements/shadcn | Fully supported (§3) | Fully supported (§3) |
| Extending the current 1,042-line vanilla-JS page | N/A | N/A — the third real option, addressed below |

- **Extending the current page** is not recommended as the M5 foundation: the M5 screen list
  (`PLAN.md:103`) — step timelines, a citation drawer, a supply-chain graph, dossier/what-changed,
  export, Freshness, Method, conversation history, an admin console — is a full multi-screen
  application, and the existing `tests/ui/` harness (pure-function extraction via `node --test`, no
  framework, `tests/ui/harness.mjs:13-18`) has scaled to M1-M4's incremental additions but was never
  a component model. Its pure-helper-function style is worth preserving conceptually inside whichever
  framework is chosen, but stretching it across M5's full scope means hand-building React's
  composition/diffing anyway.
- **Between Next and Vite**: with the "AI Elements needs Next" premise removed, the technical case
  narrows to prerendered-HTML SEO (weak value behind an Access gate) versus avoiding Next's
  static-export edge cases (real, one-developer-relevant cost) — a close call on technical merits
  alone. **Given R15 is a standing, separate owner instruction for Next.js + AI SDK, the
  recommendation is to keep Next.js static export for M5** unless the owner explicitly revisits R15
  in light of §3's finding that its original justification (AI Elements' Next dependency) doesn't
  hold. Either choice is workable per this research; this is a decision for the owner to make with
  the corrected premise in hand, not one this report should make unilaterally by defaulting away
  from a recorded ask.
- **Cloudflare side of the pairing changes regardless of Next-vs-Vite**: adopt Workers Static Assets
  or a proxied custom domain per §5, not Cloudflare Pages as originally recorded — Next.js's static
  export output deploys identically to either.

## Sources (accessed 2026-09-30)

- registry.npmjs.org: `next`, `ai`, `@ai-sdk/react`, `@ai-sdk/langchain`, `react`, `react-dom`,
  `vite`, `@xyflow/react`, `cytoscape`, `sigma`, `graphology`, `react-force-graph`,
  `@marsidev/react-turnstile`, `react-turnstile`, `axe-core`, `@axe-core/playwright`, `@lhci/cli`,
  `jspdf`, `pdfmake`, `@react-pdf/renderer`, `react-markdown`, `remark-gfm`, `resumable-stream`,
  `lighthouse`, `vercel-ai-sdk` (JSON `time`/`dist-tags`/`license` fields, queried directly); npm
  registry search API for `cloudflare turnstile` scope
- pypi.org/pypi/pydantic-ai/json, pypi.org/pypi/pydantic-ai-slim/json
- github.com (via `gh api`/`gh search`): `pydantic/pydantic-ai` (contents, pyproject.toml, `ui/`
  module tree, `_adapter.py` source), `vercel/ai-elements` (contents, package.json, LICENSE, code
  search, issue/PR search), `xyflow/xyflow` (releases, commits), `GoogleChrome/lighthouse-ci`
  (releases, commits, issues), `vercel/ai` (releases, commits), `bpampuch/pdfmake` (LICENSE,
  package.json, repo metadata), `parallax/jsPDF` (repo metadata), `vercel-labs/ai-sdk-preview-python-streaming`
  (repo metadata), `elementary-data/py-ai-datastream` (repo metadata + README),
  `keurcien/langchain-ai-sdk-adapter` (repo metadata + README, via `gh search repos`/`gh search code`),
  `lointain/langchain_aisdk_adapter` (repo metadata, noted not investigated further)
- ai-sdk.dev/docs/ai-sdk-ui/stream-protocol, /transport, /chatbot-resume-streams, /streaming-data,
  /docs/migration-guides/versioning, /docs/migration-guides/migration-guide-7-0
- nextjs.org/docs/app/guides/static-exports (version banner 16.3.7, lastUpdated 2026-08-25);
  `npm view next@16.3.7 peerDependencies`
- vercel.com/docs/plans/hobby (last_updated 2026-09-14)
- ui.shadcn.com/docs/installation/vite
- developers.cloudflare.com/pages/, /workers/static-assets/, /workers/static-assets/billing-and-limitations/,
  /workers/static-assets/migration-guides/migrate-from-pages/, /workers/platform/limits/,
  /workers/configuration/previews/,
  /cloudflare-one/access-controls/applications/http-apps/authorization-cookie/cors/
- WebSearch aggregations (labeled UNVERIFIED where not cross-checked on a primary source): React
  Flow large-graph community guidance, Turnstile preview-hostname coverage for Pages
- Repo (read-only, this session): `docs/v2/research/research-scale-frontend.md`,
  `docs/v2/PLAN.md`, `docs/v2/REVIEW_2026-09-26.md`, `docs/v2/M1_REPORT.md`,
  `src/semigraph/serve/static/index.html`, `src/semigraph/serve/guard.py`,
  `src/semigraph/serve/main.py`, `src/semigraph/config.py`, `pyproject.toml`, `tests/ui/`
