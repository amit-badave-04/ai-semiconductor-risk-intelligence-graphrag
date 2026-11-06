# M5b: frontend parity and core pages (Vite + React SPA at /v2/, then /)

Status: **APPROVED by the owner 2026-11-06** ("Approve all recommended": D1-D18 as recommended, including D17 and D18a's live rollback drill). Path: full (a new feature on the sensitive list: input handling
(the SSE parser, the answer renderer, the upload UI), external calls (Turnstile, the same-origin API), display of LLM output
and of uploaded text; plus a new Docker stage and a new npm dependency tree). Written 2026-11-06, revised the same day after
an adversarial Opus review and again after a second Opus re-review of that revision (Appendix C lists every finding and what
was done). Nothing in it is built.

Governing documents (this spec restates them for F2 and wins over them only where it says so, with the owner's approval):
`docs/v2/M5_PLAN.md` (4.7, gates section 7, Step 0 section 8, deploy section 9), `docs/v2/M5_DECISIONS.md` (it wins over
M5_PLAN on conflict, line 3; decision 7 at line 129, the nine promotion gates at line 77, decision 10 = F2 at line 155,
package A at line 154), the stack research note `docs/v2/research/m5b-stack-2026-11-06.md`, `docs/v2/M5A_BUILD_PLAN.md`
(I6, section 8), `docs/v2/M5B_TEST_CLASSIFICATION.md`, `docs/v2/research/m5-frontend-inventory.md` (section 1 is the parity
list of record). Start signal: the owner's 2026-11-06 "fix the small follow-ups, then start m5b" is the go to start M5b
(this spec); it is not an approval of this spec and not a record that M5a's gates passed (S2 was not attempted, decision 20
option B, M5_DECISIONS.md:213; the paid path, meter, restart-mid-stream and drain-with-open-stream were not checked live,
:241). One M5a item is also unmet, found by the review of this spec: gate A4 (M5_PLAN.md:160) requires `/openapi.json` to
answer 404 and M5_PLAN.md:33 says it was closed in M5a Step 0, `main.py:444-445` at HEAD ae84a89 (live v15) set only `docs_url=None,
redoc_url=None`, so the route is served on live; commit 568d7fa adds `openapi_url=None` (`main.py:445`) with a test (`tests/test_serve_lifespan.py:550-561`), live with the I4 deploy.

## Why

- The only page is `src/semigraph/serve/static/index.html`: 1,097 lines (`wc -l`, 2026-11-06), one inline `<script>`, served by `GET /`
  (`routes.py:171-175`). Five views (Ask, Company, Workspace, Freshness, Method) do not fit in it. The owner chose, on
  2026-10-03, F2: Vite + React, a thin typed client over the existing SSE grammar, no AI SDK, no `/api/chat` encoder
  (M5_DECISIONS.md:155), served same-origin from the existing image at an unlinked `/v2/` and promoted to `/` only on the
  owner's written sign-off (H1, :74; M5_PLAN.md:177).
- Keyboard-only visitors cannot open the evidence drawer today: citation chips are mouse-only spans (`index.html:278`);
  `#status` has no live region, `#q` has no label (checked 2026-11-06; `208364a` fixed only the drawer and the drop zone).
- Nothing of M5b exists: no `web/`, `scripts/export_ids.py`, `tests/data/sse/`, `docs/v2/M5A_SSE_CONTRACT.md`,
  `UI_V2_PREVIEW`/`UI_V2` settings, `GET /api/config`, `/legacy`, static mount or HTML header middleware (ls and grep of
  `config.py` and `src/semigraph/serve`, 2026-11-06).
- The plan needs re-stating because the facts moved after M5_PLAN: decision 10 removed the encoder, so B1 ("eight AI-stream
  fixtures") and spike S3 have no object; the 2026-11-06 stack re-verification found that the CSP's `base-uri 'none'`
  refuses a `<base>` element (so the injected `<base href="/v2/">` of M5A_BUILD_PLAN.md:106 is expected to fail; by
  inference from MDN, not browser-tested), that the CSP is attached only inside the `GET /` handler, that Starlette's
  `StaticFiles(html=True)` falls back to `404.html`, never to `index.html` (`staticfiles.py:147-149`, starlette 1.6.0), and
  that FastAPI 0.141.1's own `app.frontend()` does serve an `index.html` fallback but gives no way to add response headers
  (evaluated in R9), that the live renderer supports headings, lists and bold (so "plain text + chips" would regress it),
  and that TypeScript is now 7.0 (no programmatic API). Appendix B lists the stale statements.

## Scope

**In.**
1. `web/`: Vite + React + TypeScript SPA, lockfile, pinned Node; thin typed SSE client over `/api/ask` and the upload-job
   stream; no AI SDK, no `/api/chat`.
2. Pages: Home/Ask (`/`), Company (`/company/<TICKER>/`), Workspace (M4 parity), Freshness, Method & Evidence.
3. Own restricted answer renderer `AnswerText` (no markdown engine, no HTML path), badges, checks, chips, evidence drawer,
   steps timeline, with the old page's tests ported; component-level XSS fuzz; the accessibility items of promotion gate 3.
4. Serving: flags `UI_V2_PREVIEW` / `UI_V2` (in `fly.toml [env]`), `/v2/` (noindex), `/legacy`, `GET /api/config`, Dockerfile
   web stage, CI `web` job, one-artifact base-path design, CSP unchanged.
5. B1 restated: recorded SSE byte fixtures, `docs/v2/M5A_SSE_CONTRACT.md`, client tests.
6. Promotion gate 1: every old assertion and pin mapped to a new test or a written obsolete reason (refreshed
   classification plus a machine check).
7. A fixture server (fakes, no Neo4j, no LLM) for Playwright, axe and Lighthouse; S8 baseline of the old page.
8. Docs: `web/README.md`, a RUNBOOK section, the contract doc, refreshed classification, `artifacts/m5b_*.json`.
9. `/openapi.json` (`openapi_url=None`, D16): M5_PLAN.md:33 and gate A4 (:160) promised it closed; closed in code by 568d7fa, live at I4.
   An unmet M5a gate, not an M5b feature.

**Out.** Cloudflare, Workers, Access, a domain (M6, M5_DECISIONS.md:74); MCP, API keys, admin console, export, follow-up
turn, supply-chain map (M5c; MCP and keys wait for a named consumer, :158); the "N risks dropped" badge (omitted, decision
9, :79); Next.js and the AI SDK (fallback only, D3); any change to the `/api/ask` grammar, `template_fingerprint()`
(4d0a62f5a0, pinned in three places), `guard.py` and the bot check, `stream_runtime.py`, `retrieval/`, `state/`,
`workspace_routes.py`, `dossier_routes.py`, `src/semigraph/eval/agent_eval.py`; a degraded `retrieval_only` stream (D12);
surfacing server fields the old page never shows (for example `workspace.suspicious`); web fonts, a service worker,
analytics or any third-party origin; staging windows and load tests; the 1,000-user claim on any surface.

## Gate map (B1-B7 of M5_PLAN.md:162 and the nine promotion gates P1-P9 of M5_DECISIONS.md:77; nothing is waived)

| Gate | Restated check in this spec | Evidence |
|---|---|---|
| B1 (restated, needs approval) | The eight AI-stream fixtures and `/api/chat` byte tests are void with the encoder (decision 10). Replacement: R15-R18 SSE fixtures recorded from the real routes, a Python byte test, and vitest client tests decoding each fixture to an `.expected.json` | `tests/data/sse/*`, `tests/test_sse_fixtures.py`, `web/src/lib/__tests__/sse*.test.ts` |
| B2 ~ P1 (+ part of P6) | Parity list (inventory section 1) mapped test by test; `ids.generated.ts` current; `/v2/company/NVDA/` and every deep link resolve | `docs/v2/M5B_TEST_CLASSIFICATION.md` + checker (R29), `tests/test_export_ids.py`, `tests/test_serve_ui_v2.py` |
| B3 ~ P3 | `lighthouse` accessibility >= 95 and best-practices >= 90 on five pages x mobile and desktop against the FastAPI app (fixture server); axe zero serious/critical (WCAG 2.2 AA tags, see Acceptance); scripted keyboard tests; one manual pass; the five named items (A1-A5) | `artifacts/m5b_lighthouse.json`, `artifacts/m5b_axe.json`, `web/e2e/*`, the manual-pass record |
| B4 ~ P2, P4 | CSP byte-identical on every HTML response (S5); no HTML sink, no markdown/HTML renderer on answer paths (S1, S2); XSS fuzz on rendered components; `npm audit` no high; Turnstile: the server stays the fail-closed gate, one widget in the app shell, reset after every use, one live widget under StrictMode and remount (R33) | tests named in S1-S8 |
| B5 ~ P9 | CI green (new `web` job and both Python selections); reproducible image (`npm ci`, lockfile, pinned Node, digest-pinned base) | CI run, `tests/test_dockerfile_web.py` |
| B6 ~ owner written sign-off + P8 | Click-through on `/v2/` of the live app confirmed in writing, named buyers' feedback, before `UI_V2=true`. B6 says "preview path/staging"; this spec narrows it to the live preview path because no staging window is planned (D1) | the owner's message, recorded in Status |
| B7 | API spend <= $5 | Cost section |
| P5 (no B gate) | One artifact served at `/v2/` and, flipped, at `/`: the same `dist` loads both with zero console errors and zero CSP violations (R12) | Playwright against the fixture server with both flag states |
| P6 (no B gate) | Deep links; in-flight workspace continuity with `/legacy` (same origin and `sessionStorage`; "in-flight" defined in D18); a one-deploy rollback, tested live at I4 (D18) | `web/e2e/continuity.spec.ts`, the R7 state-table pytest, the I4 drill with timings in `artifacts/m5b_rollback_drill.json` |
| P7 (no B gate) | `/legacy` kept through the M6 cutover | R11, D2 |

Conflict resolved here, for the owner to confirm (D2): B6 and M5_PLAN.md:218 say the old page is removed after a second
written confirmation; P7 (approved later, in package A) says `/legacy` stays through the M6 cutover. P7 wins
(M5_DECISIONS.md:3). `serve/static/index.html`, `tests/ui/`, `tests/test_static_ui.py` and `tests/test_serve_agent_ui.py`
stay until M6 decides their removal as its own item.

## Requirements

**Build and reproducibility**

1. **R1 Stack and pins.** `web/` is Vite + React + TypeScript. `package-lock.json` is committed. `web/.npmrc` holds
   `save-exact=true` and `ignore-scripts=true`, pinned by a test; every version in `package.json` is exact; CI, the
   Dockerfile and every later install use `npm ci --ignore-scripts`. The lockfile is created once, inside
   `node:24.22.2-slim` on Linux with `npm install --ignore-scripts`, not on this Windows desktop (Node 26.13.0: fine for
   development, not the reference). A Windows-resolved lockfile may omit the Linux native binaries (inferred), so a test
   asserts the lockfile holds `linux-x64-gnu` entries for rolldown, lightningcss and `@tailwindcss/oxide`. `ignore-scripts`
   stops lifecycle scripts only: `npx shadcn@4.23.0` and the Playwright browser download still execute code, which is why
   downloading and running them needs the owner's go (D17; the stack note held it back, section "What was NOT done",
   line 10). Node 24.22.2 is written in `web/.node-version`, the Dockerfile tag, the CI `web` job and the two existing
   `setup-node` steps (`tests.yml:37-39`, `:71-73`, now `"24"`), and a test asserts all five agree; `engines.node` is the
   floor `>=24.22.2`. Starting pins, registry-reported on 2026-11-06 and NOT installed (the scaffold re-verifies against the
   registry and the lockfile decides): vite 8.3.6, @vitejs/plugin-react 6.2.1, react and react-dom
   19.3.1, typescript 7.0.2, oxlint 1.91.0, react-router 8.5.1 (declarative mode; `react-router-dom` no longer exists),
   tailwindcss and @tailwindcss/vite 4.3.4, shadcn CLI 4.23.0 with Base UI (`@base-ui/react` 1.9.0), eventsource-parser
   4.1.3, vitest and @vitest/coverage-v8 5.1.0,
   jsdom 30.1.3, @testing-library/react 16.3.3 + dom 10.4.2 + user-event 14.6.8 + jest-dom 7.0.1, @playwright/test 1.65.0,
   @axe-core/playwright 4.14.0, lighthouse 13.6.0 (source: stack note section 7). There is no Turnstile wrapper package: an
   own small hook does it (the old page needs four lines, `index.html:821-824`, and a wrapper would inject the third-party
   script from a dependency); smoke check 7 confirms it under StrictMode. The list is not the whole tree: shadcn's Base UI
   init adds the devDependencies `tw-animate-css` and `shadcn` and the CSS imports `@import "tw-animate-css"` and
   `shadcn/tailwind.css` (read from `shadcn-ui/ui` `apps/v4/registry/bases/base/registry.ts` with `gh search code`,
   2026-11-06), and installs lucide-react, tailwind-merge, clsx and class-variance-authority (stack note line 198);
   @types/react and @types/react-dom are also needed (reported by the review, not checked). The scaffold writes the full
   resolved list with licenses to `artifacts/m5b_smoke.json`. The `tw-animate-css` import is dropped at scaffold (nothing
   moves, R30); the CSS scan proves no keyframes ship from either import. Only the shadcn components actually used are
   installed (M5_PLAN.md:125's "all shadcn components installed once" is narrowed to the used set, all in main's I1 commit).
   `chart` is not installed: it is the one component of the 64 in shadcn's Base UI set that contains
   `dangerouslySetInnerHTML` (read from `shadcn-ui/ui` `main` on 2026-11-06 with `gh api`; `ChartStyle`, line 94). Any
   copied-in component that contains an HTML sink is not installed or is rewritten, so S1 holds with no exclusion.
2. **R2 Smoke list first.** The first task of I1 is the stack note's section 8 list (nine checks, none run yet), with check 9
   replaced (one artifact, no second build): the same `dist` is served at `/v2/` and, flipped, at `/`. Results go to
   `artifacts/m5b_smoke.json`. A failed check is reported as failed. If check 5 shows TS 7 breaks shadcn or oxlint,
   `typescript@~6.0.3` is allowed for tooling only (reported). A failure that is the framework's fault feeds D3.
3. **R3 Language and lint.** TS strict, `paths` only (no `baseUrl`, a hard error in TS 7); `tsc -b` is part of the build.
   oxlint with the `jsx-a11y`, `react` and react-hooks plugins enabled and the rules `react/no-danger`, `no-eval`,
   `no-implied-eval`, `no-new-func` as errors (exact rule names verified at scaffold). `typescript-eslint` is not used (it
   does not support TS 7).
4. **R4 Build output compatible with the unchanged CSP.** `tests/test_dist_contract.py` runs in the `web` job after the build
   (skipped, and reported as skipped, when there is no `web/dist`) and checks: `dist/index.html` has no inline `<script>`,
   no `<style>`, no `style=` attribute, no `<base>` and no `<meta name="robots">` (the same artifact is served at `/`, so a
   baked-in robots tag would hide the promoted page); no `data:` font (`assetsInlineLimit` set so fonts are never inlined;
   the CSP has no `font-src`, so a `data:` font is blocked) and no `@font-face` in `dist/**/*.css` (system font stack, as
   `index.html:10`); no absolute URL in `dist/index.html` except `https://challenges.cloudflare.com`, and no absolute
   `url(...)` in `dist/**/*.css`. The JS bundles are not scanned for URLs: react-dom@19.3.1 itself contains
   `http://www.w3.org/2000/svg`, `/1999/xlink`, `/1998/Math/MathML`, `/XML/1998/namespace` and `https://react.dev/errors/`
   (jsDelivr, re-read 2026-11-06). "No other origin" is enforced at runtime by S5's request-origin check.
5. **R5 Generated constants.** `scripts/export_ids.py` writes `web/src/lib/ids.generated.ts` (the four id patterns of
   `retrieval/ids.py:22-25` and the anchored forms generated from the `*_RE` objects at `:32-35`) and
   `web/src/lib/pages.generated.ts` (the SPA page paths from `serve/ui_v2.SPA_PAGES` and the `{ticker, name}` list of
   `universe.FILERS`, replacing the plan's `universe.json`). The anchored forms are translated, not copied: Python ends
   them with `\Z`, which in JS is a literal `Z` without the `u` flag and a syntax error with it (standard regex rules, not
   run here); the generator writes `$` (no `m` flag). `CHUNK_ID_RE` is stricter than `CHUNK_ID_PATTERN` (`{10,30}` before
   the first colon, `ids.py:32`) and the old page's `ID_FORMS` lack that bound (`index.html:146`), so a chip with a 9- or
   31-character chunk prefix makes no request on the new page; the classification records this difference.
   `tests/test_export_ids.py` regenerates in memory and diffs (a stale file is red). A cross-engine test runs one shared
   corpus (`tests/data/citation_corpus.json`: valid ids, near-misses, adversarial strings, a trailing newline, `%0A`, 9- and
   31-character chunk prefixes) through Python `CITE_RE.findall` and the TypeScript tokenizer, and through Python
   `classify_id` and the TypeScript classifier, and requires identical results.
6. **R6 Reproducibility docs.** `web/README.md` has the exact commands (install Node 24.22.2, `npm ci --ignore-scripts`, dev
   server with `/api` proxied to a local uvicorn, build, unit, e2e, lint, audit, Lighthouse); `docs/RUNBOOK.md` gets a
   "Frontend: flags, flip, rollback" section holding the exact commands of R7 (this spec states what the section must
   contain; the RUNBOOK is written in I1/I4); derived results go to `artifacts/m5b_smoke.json`, `m5b_lighthouse.json`,
   `m5b_axe.json`, `m5b_page_weight.json`, `m5b_licenses.json`, `m5b_rollback_drill.json`. The `.env.example` lines for the
   three new settings are added by the owner (the file is read-denied to the agent; same hand-off as
   `docs/specs/m5a-followups.md` requirement 3).

**Serving**

7. **R7 Settings and states.** New settings `ui_v2_preview` (env `UI_V2_PREVIEW`), `ui_v2` (`UI_V2`), both default false, and
   `ui_dist_dir` (`UI_DIST_DIR`, default `/srv/web`). The surface per state:

   | `UI_V2_PREVIEW` | `UI_V2` | `/` | `/legacy` | `/v2/*` | root pages |
   |---|---|---|---|---|---|
   | false | false | old page | 404 | 404 | 404 |
   | true | false | old page | old page | new SPA, `noindex` | 404 |
   | any | true | new SPA | old page | new SPA, `noindex` (alias) | new SPA |

   Both false is today's surface (rollback) plus `Cache-Control: no-cache` on `/` (R10) and `GET /api/config`.

   Where the flags live: in production `fly.toml [env]`, as non-secrets like `AGENT_ENABLED` (`fly.toml:43`);
   `scripts/check_env_fly.py` reads `[env]` (`:13`) and warns when a name is in both `[env]` and a secret (`:27-28`), so
   they are never secrets. Commands (the RUNBOOK section carries them; tags come from `flyctl releases -a semigraph`):
   - image change (the preview deploy, I4): edit `[env]`, `uv run python -m scripts.check_env_fly`,
     `flyctl deploy --ha=false --remote-only --yes` (RUNBOOK.md:26, :134; builds the web stage);
   - flag flip or rollback on the same image (I4 drill, I5): edit `[env]`, `check_env_fly`, then
     `flyctl deploy --image registry.fly.io/semigraph:<tag now running>` (RUNBOOK.md:139, which gives it without
     `--ha=false --yes`; add them as at :26). That `--image` applies the edited `[env]` is inferred; the I4 drill checks it
     with `flyctl ssh console -a semigraph -C "printenv UI_V2_PREVIEW"` (as RUNBOOK.md:211-212) and `/v2/` answering 404.
     If it does not, a flag rollback is the full deploy above, and the drill times that instead;
   - image rollback: the same command with the previous tag (v15: `registry.fly.io/semigraph:m5a-d21-2afdd6e`,
     M5_DECISIONS.md:233).
   Every deploy replaces the one machine and drains it first (RUNBOOK.md:426-429).
8. **R8 `GET /api/config`.** Returns `{turnstile_site_key}` only (one key), `Cache-Control: no-store`, always served: it is
   registered unconditionally, on its own `APIRouter` in `ui_v2.py` that `create_app()` includes whatever the dist state, so
   it sits outside the R13 `index.html` check that gates the page, asset and root-page routes. It answers 200 with its one key
   with both flags off (R7), with the dist directory absent or without `index.html` (R13) and in I1's commit with no `web/`
   (no route of that name exists today: grep of `*.py`, `*.html`, `*.toml` and `*.yml` for `api/config`, 2026-11-06). R9(d)
   concerns the page and asset routes, which never answer `/api/*`; this one exact path is the only API route in
   `ui_v2.py`. The two flags are not exposed: the client takes its basename from the path (R12), and a public route on the old site should
   not announce that a preview exists. The site key is public by design (it is in the served page today). A test pins the
   key set. The legacy page keeps its `__TURNSTILE_SITE_KEY__` substitution untouched. Client behaviour: a 200 with an empty
   key is a known "no key" (no widget, asks sent without a token, as the old page does when `TURNSTILE_SITE_KEY` is empty,
   `index.html:800, :821`); a failed fetch (network error or non-200) is retried once, then an error is shown and no
   widget mounts. "Unknown" is never treated as "no key". Asks are still sent either way (R33): the server is the gate.
   The fixture server returns an empty key by default (so Lighthouse loads nothing third-party) and a dummy key string,
   not a Cloudflare key, when the Turnstile specs start it with `--site-key`.
9. **R9 Routes and order.** A new `serve/ui_v2.py`, included LAST in `create_app()` (after `workspace_routes.router`;
   edits to `main.py`, `config.py`, `routes.py` are main-only). It is our own small router, not FastAPI's built-in
   `app.frontend()` (evaluation below; D6). Rules, each with a test:
   (a) `/v2/assets/*` is served by a Starlette `StaticFiles` subclass built with `check_dir=False` (Starlette 1.6.0 keeps
   `follow_symlink=False` and checks containment with `commonpath`, `staticfiles.py:47, 154-166`) and answers
   `Cache-Control: public, max-age=31536000, immutable`; it is mounted BEFORE the page routes; a missing asset is a 404,
   never `index.html` with 200.
   (b) File lookup cannot leave `ui_dist_dir`: `..`, `%2e%2e`, `%2f`, backslash and NUL answer 404 and never read a file
   outside it.
   (c) Any other `/v2/<path>` whose last segment has no file extension returns `index.html` through `html_response()`
   (200) so the SPA renders its not-found view for an unknown route. One with an extension is looked up in the dist root
   (the Vite `public/` files) under rule (b) and is a 404 if absent; `.html` and `.htm` always answer 404, `/v2/index.html`
   included, so no `text/html` can leave the file branch without the CSP.
   (d) `/api/*` is never answered by these routes: an unknown API path stays a JSON 404 (`{"detail": ...}`), with the flags
   on or off. There is no root catch-all.
   (e) A redirect (the non-slash form of a page path, R11) is built from a fixed template of the enumerated page path,
   never from the raw request path.

   *FastAPI's built-in SPA support, evaluated on the installed source* (`.venv`, fastapi 0.141.1, starlette 1.6.0;
   `FastAPI.frontend(path, *, directory, fallback="auto"|"index.html"|"404.html"|None, check_dir="auto")`,
   `applications.py:1222`, `routing.py:2630`; the docs page is fastapi.tiangolo.com/tutorial/frontend/). For: it registers
   low-priority routes checked after every path operation (`routing.py:2567, 2710`); it uses `follow_symlink=False`
   (`:1917`) and a normalised path (`:1853-1860, :1938`); a non-GET/HEAD method gets 404 or 405 (`:1947-1950`); the
   `index.html` fallback applies only to navigation requests, by `Accept` (`:2037-2044`), so a missing script or stylesheet
   request is a 404. Against: (1) it has no header hook: every file, directory-index (`/v2/` itself, `:1955-1959`) and
   fallback response is a plain `file_response` (`:1959, :2015`), so the CSP and `noindex` would need the HTML-only
   middleware D6 rejects, or a subclass of the private `_FrontendStaticFiles`, and the immutable asset header likewise;
   (2) `fallback="auto"` prefers a `404.html` when the dist has one (`:1961-1964`); (3) `Accept` gates the fallback, so
   TestClient's default `*/*` gets a 404 and a browser navigation to a missing `/v2/assets/x.js` gets `index.html` with
   200, which breaks (a); (4) `check_dir="auto"` is True unless `FASTAPI_ENV=development` (`:1881-1896`), and construction
   then raises when the directory is absent (`:1908-1912`), the same boot failure as R13; with `check_dir=False` and no
   `index.html` a navigation raises and answers 500 (`:2009-2014`); (5) the flipped root pages must stay enumerated:
   `frontend("/")` would answer an unknown `/api/nope` sent with `Accept: text/html` with `index.html` 200, against (d);
   (6) its routes sit in `_low_priority_routes`, not in the route table (`:2567, 2710, 2803-2809`), so S5's walk cannot see
   them, as with a Mount. Decision: keep our router, because making `frontend()` safe needs the middleware D6 rejects or
   private classes, and root pages need custom routes anyway. Alternative if the owner prefers it:
   `frontend("/v2", directory=..., fallback="index.html", check_dir=False)` registered only when `index.html` exists at
   boot, with a pure-ASGI header middleware limited to `/v2/`; R9 and S5 test behaviour, so they stay unchanged. Whichever
   is built, navigation tests send `Accept: text/html`. All of this is from reading the source, not from running it.
10. **R10 HTML responses.** One helper, `ui_v2.html_response()`, builds every HTML response. It builds a NEW dict,
    `{**routes.SECURITY_HEADERS, "Cache-Control": "no-cache", ...}`, and never mutates the shared one (adding
    `X-Robots-Tag` to the module-level dict would put `noindex` on the live `/` after the first `/v2/` request): `noindex`
    only on `/v2/` pages. `/` and `/legacy` get `Cache-Control: no-cache` and no `X-Robots-Tag`. JSON and SSE responses are
    untouched (no CSP added, as today). This deviates from the "pure-ASGI header middleware" of M5_PLAN.md:125 and
    M5A_BUILD_PLAN.md:106: see D6. Test, in both request orders (`/v2/` then `/`, and `/` then `/v2/`, in one process):
    `/` and `/legacy` never carry `X-Robots-Tag` and the shared dict is unchanged.
11. **R11 The flip.** `routes.index` (the `GET /` handler) delegates: legacy body when `ui_v2` is false, the SPA `index.html`
    when true; the legacy body also serves `/legacy` through the same function (same substitution, same headers). When
    `ui_v2` is true the page paths of `SPA_PAGES` are served at the root (`/`, `/company/{ticker}/`, `/workspace/`,
    `/freshness/`, `/method/`; the form without the trailing slash redirects to it, tested); any other root path stays a
    404. `/v2/assets/*` stays mounted after the flip (the build bakes the `/v2/` base into asset URLs, R12), and `/v2/...`
    page URLs keep working as noindex aliases, so a previewer's bookmarks and a one-deploy rollback keep working; a redirect
    of `/v2/` to `/` is an M6 item.
12. **R12 Base path: one artifact.** Vite `base` is the absolute `/v2/`; the build is produced once. The router `basename` is
    chosen at runtime from the path (`/v2` when `location.pathname` starts with `/v2/`, else empty), NOT from
    `import.meta.env.BASE_URL` (always `/v2/`) and NOT from a `<base>` element (refused by `base-uri 'none'`, inferred).
    Relative `./` assets would resolve under `/v2/company/NVDA/` and 404 (inferred). UNVERIFIED until the Playwright test
    loads the same `dist` through the fixture server at `/v2/company/NVDA/` and, with `UI_V2=true`, at `/company/NVDA/`,
    each with zero console errors, zero `securitypolicyviolation` events and every asset 200.
13. **R13 Fail-safe, memory, image.** The v2 page, asset and root-page routes (not `GET /api/config`, which R8 registers
    unconditionally) are registered only when `ui_dist_dir/index.html` exists and is readable
    while `create_app()` runs (`app = create_app()` runs at import, `main.py:456`, so a constructor that raises takes the
    whole app down: Starlette's `StaticFiles` raises when its directory is absent, `staticfiles.py:55-56`, which is why
    R9 builds it with `check_dir=False`). When the dist is absent or unreadable: one WARNING is logged; `/v2/*` answers a
    JSON 404; `/` serves the old page even with `UI_V2=true` (the state table falls back to its first row); `/healthz` is
    unchanged, so a frontend fault cannot fail Fly's health check (`fly.toml` `[checks.health]`, path `/healthz`) and
    restart the machine. A frontend fault must never take down the old page (a safeguard that must not block, as in the
    memory-guard spec). Tests: `create_app()` with `ui_dist_dir` absent, and with the directory present but no
    `index.html`; each also checks that `GET /api/config` still answers 200 with exactly its one key and `no-store`, and that
    an unknown `/api/*` path stays a JSON 404. No Node, npm or `node_modules` in the runtime image; assets are streamed from disk (the uvicorn peak was
    predicted at 2,385,128 kB against the 2,400,000 kB limit, M5_DECISIONS.md:208, and measured on three W1c staging boots
    of fresh shared-cpu-2x 4 GB machines with a mock LLM at 2,315,528-2,322,556 kB, margin 3.2%, :220, not on the live
    machine: the web build must add nothing resident). Dockerfile: `FROM node:24.22.2-slim@sha256:<digest> AS web`, `COPY web/package.json
    web/package-lock.json`, `npm ci --ignore-scripts`, `COPY web/`, `npm run build`; runtime
    `COPY --from=web --chown=app:app /web/dist /srv/web` AFTER the model COPY (Dockerfile:62) so a web change does not
    invalidate the model layer; `.dockerignore` gains `web/node_modules` and `web/dist` (the existing `dist/` entry is
    inferred, from the dockerignore matching rules, to match only the root). The web stage lands in the same commit as `web/`.
14. **R14 CI.** A new `web` job (Node from `web/.node-version`; `actions/setup-node` moved off v4, current v7.1.0 per the
    stack note). The job also sets up Python as the unit job does (`astral-sh/setup-uv@v5`, `uv python install 3.13`,
    `uv sync --extra serve --extra agent`, `tests.yml:34-41`) because the fixture server is Python. Steps: `npm ci
    --ignore-scripts`, oxlint, `tsc -b`, vitest with coverage thresholds, `npm audit --audit-level=high`, build,
    `tests/test_dist_contract.py` (R4), `playwright install --with-deps chromium` (command not re-verified), Playwright +
    axe and Lighthouse against the fixture server, and `docker build --target web` (Docker on the runner is inferred). The
    existing Python selections must not need `web/dist`: `test_serve_ui_v2.py` builds a tmp dist fixture.

**Client and the SSE contract (B1 restated)**

15. **R15 Fixtures.** `tests/data/sse/` holds byte fixtures (frames separated by a blank line, `sep="\n"`), recorded from the
    real routes with fakes at $0 and pings disabled by the recorder (no production change): `cached_done.sse`,
    `live_hybrid.sse`, `agent_steps.sse`, `escalated.sse`, `workspace_done.sse`, `error_before_delta.sse`,
    `error_after_delta.sse`, `job_ready.sse`, `job_failed.sse`; `refusals.json` records the pre-stream JSON refusals with
    their real `MSG_*` text (400, 403, 404, 429, 503 including draining with `Retry-After: 30` and `retrieval_only`); two
    hand-written fixtures, labelled hand-written: `ping_interleaved.sse` proves ping frames are ignored, and
    `done_max_size.sse` is a `done` frame at the server's largest answer (2,400 tokens, `LLM_ANSWER_MAX_TOKENS`,
    `fly.toml:30`, plus the citation list and checks; no real answer is reliably that long) and sizes `maxBufferSize` (R17).
    Why the count is not eight:
    the degraded fixture went with `/api/chat` (`retrieval_only` is a 503 on `/api/ask`, `routes.py:349-350`) and the
    AI-SDK part shapes (`start`, `finish`, `[DONE]`, `source-document`) no longer exist. If the production path cannot
    produce `error_after_delta`, the worker reports why and the fixture is dropped with the owner's knowledge; it is never
    hand-written as if recorded. Each `.sse` has an `.expected.json` sidecar of the decoded events, generated in Python from
    the event dicts the route yielded before serialisation (never by the TypeScript decoder under test, so the vitest
    comparison joins two independent paths).
16. **R16 Recorder and byte test.** The write path is a mode of `tests/test_sse_fixtures.py` itself
    (`RECORD_SSE=1 uv run pytest tests/test_sse_fixtures.py` rewrites the files instead of comparing; the default mode
    re-records in memory and compares bytes, so a fixture cannot go stale). The test and the recorder share one app builder,
    `tests/serve_app_factory.py` (main, I1), because `tests/test_serve_api.py::client` is a pytest fixture (it depends on `fakes`) and
    cannot be called from a script: it is a hand-built FastAPI app (`routes.router`, fake settings, a fake state backend,
    `routes.aanswer_stream` patched, `test_serve_api.py:147-168`). Whether `test_serve_api.py` is refactored onto the factory is
    worker A's call; it must stay green and `CACHED_DONE_BYTES` (`:307`) stays untouched. The pings are disabled through the
    recorder (the ping interval patched in the test process, no production change). The agent clock is frozen the same way,
    by patching `semigraph.agent.graph._now` (`graph.py:51`, a module attribute kept patchable for tests), so
    `done.agent.elapsed_s` is a real, deterministic float: the pre-M5 recorder's `"<elapsed_s>"` placeholder
    (`tests/data/record_events_pre_m5.py:58`) is not used, because a string is not what the route sends and S6's
    `Number.isFinite` check would reject it. The agent and workspace fixtures run the
    real async twins over the fakes the pre-M5 replay recordings used (`tests/agent_fakes.py`,
    `tests/data/agent_events_pre_m5.json`, `workspace_events_pre_m5.json`); where a case cannot be built that way the worker
    reports it (UNVERIFIED until I2).
17. **R17 Typed client.** `web/src/lib/events.ts` (a discriminated union over `retrieval | step | delta | escalated | done |
    error`, the cached and live `done`, and the job event, validated by type guards, not casts) and `sse.ts` (`fetch` POST +
    `eventsource-parser`; no retry of a POST, ever; `ended` (a `done` or `error` arrived) versus `failed` (the page threw)
    tracked so the status is always true, as `STREAM_CUT_STATUS` / `PAGE_FAILED_STATUS` do today; a cut stream never
    re-sends; unknown events and unparseable data are ignored). The parser is created with an explicit `maxBufferSize`:
    eventsource-parser 4.1.3 has no default limit (`@defaultValue undefined (unbounded)`, `src/types.ts:135-137`, read from
    jsDelivr 2026-11-06), so "limits stay on" would have been false. The value is chosen in I2 above `done_max_size.sse`
    with margin and recorded in the contract doc (the README's `1024 * 1024` is an example, not a default). A
    `max-buffer-size-exceeded` error sets `failed` (the page-failed status, never a silent drop); the parser is then
    terminated and its next `feed()` throws until `reset()`, so the client catches that throw and stops reading. The 16
    tests of `tests/ui/stream_cut.test.mjs` are ported, not dropped.
18. **R18 Contract doc and freeze.** `docs/v2/M5A_SSE_CONTRACT.md` is written from the fixtures (request, wire grammar, the
    six events and the cached `done`, `error` as the client sees it (`detail` only), pre-stream JSON refusals, headers pinned
    by `tests/test_serve_api.py:316-317`, the job stream). "Frozen" means: a later change to the `/api/ask` bytes needs a spec
    amendment, a re-record and the owner's approval, because the legacy page reads the same bytes.
19. **R19 Job stream client.** `GET /api/workspace/{ws}/jobs/{job_id}` with `X-Workspace-Token`: `event: job`, states
    received, validating, parsing, chunking, comparing, indexing, embedding, then `ready` or `failed`; retries on 429, 5xx
    and an early end; a 404 means gone; at most three live watchers per (workspace, job), a fourth gets 429
    (`uploads/jobs.py:40-41`, `workspace_routes.py:513-516`), so the client keeps at most one per job (R34).
20. **R20 Refusals.** A non-OK response is rendered from its JSON `detail` (400, 403 with the extra bot-check clause,
    404, 429, 503); the `MSG_*` copy is not edited; no `Retry-After` countdown or other new behaviour is added.

**Renderer and pages**

21. **R21 `AnswerText`.** The old `renderMarkdown` grammar (`index.html:275-290`) as React elements, nothing else: lines
    starting `-` or `*` are `ul/li`, `#` to `####` are `h3`, other non-empty lines are `p`, `**x**` is `strong`, a citation
    token from the generated tokenizer is a chip `<button>`; everything else is a text node. No parser library, no HTML
    string, no `dangerouslySetInnerHTML`. A transitional differential test feeds every recorded answer and the fuzz corpus
    through the old function (loaded with the existing `tests/ui/harness.mjs` loader) and through `AnswerText`, and requires
    the same structure after normalising (`b` to `strong`, chip `span` to `button`). It goes when `/legacy` goes.
22. **R22 Ask page parity** (inventory 1.1, 1.4, 1.5, 1.8). Examples panel (`GET /api/examples`), strategy select with
    `agent` present only when `/api/stats.agent_enabled`, ask button, Ctrl/Cmd+Enter, the limits hint, the withdrawn-accuracy
    notice, the agent hint and tracing note, the steps timeline, the status line (true on every exit), badges and checks,
    the reliability note and the cost note. Copy pinned by tests today survives (R30). Turnstile reset and `/api/stats`
    reload after every ask, success or failure.
23. **R23 Workspace-open behaviour.** While a workspace is open the examples panel is disabled and the strategy select offers
    only `hybrid` (M5_PLAN.md:125 keeps the first; the second removes a guaranteed 400, `guard.py` `WORKSPACE_STRATEGIES`;
    today every example click becomes a paid live call, inventory 1.4). Asks as of a version carry `as_of`. Test:
    `web/src/pages/__tests__/ask-workspace.test.tsx` (panel disabled, select offers only `hybrid`, `as_of` sent).
24. **R24 Evidence drawer.** One component owns open, close and focus; it ports `openDrawer`/`closeDrawer`
    (`index.html:747-760`) and is non-modal like today's `<aside id="drawer" inert aria-hidden="true">` (`index.html:120`),
    see A5. A stale response never fills the drawer (the old `openSeq` rule, `index.html:765`, with its test). `doc:` ids resolve through `GET /api/workspace/{ws}/evidence/{doc_id}`, other ids through
    `GET /api/evidence/{id}` (types chunk, xbrl, fr). Evidence text is a text node.
25. **R25 Company page** `/company/<TICKER>/`: `GET /api/company/{ticker}/dossier` (blocks `company`, `data_as_of`,
    `filings`, `metrics`, `active_risks`, `edges`, `rules`) and `/risk-changes`. The page keeps the distinction the header copy
    pins: filing-backed items open evidence chips; `rules` are labelled keyword matches to external Federal Register rules.
    The switcher lists the 13 `FILERS`. A 404 `unknown ticker` renders the not-found view (D8). The router-decoded
    `:ticker` is untrusted: the page fetches only when it matches a ticker of `pages.generated.ts`, always through
    `encodeURIComponent`; any other value renders the not-found view with no request. Test: `/company/..%2F..%2Fapi%2Fadmin%2Fstate`
    makes no request.
26. **R26 Freshness page** from `GET /api/freshness` (and the `freshness` field of `/api/stats` the old header line uses);
    every field of the recorded fixture is shown or explicitly omitted in the classification.
27. **R27 Method & Evidence page** (D7): how an answer is built and checked, what the badges and checks mean, verbatim
    evidence versus keyword-matched rules, how freshness is shown. Every sentence comes from copy already pinned by tests or
    is listed for the owner's review; it carries no new claim and no limits list (public surfaces state strengths only).
28. **R28 Workspace page** (inventory 1.7): create, restore (id and token), delete; upload drop zone and a real button; one
    file at a time, each awaiting its job's terminal state (R19, R34) and, under R33, a token when a site key is set (up to
    15 s, as `index.html:591, 1005-1008`); per-job rows with embedding progress; documents and versions; ask as of a version;
    the what-changed view (chips inside it open the same drawer); hidden until `/api/stats.uploads_enabled`.
29. **R29 Test migration (P1).** `docs/v2/M5B_TEST_CLASSIFICATION.md` is refreshed to the inventory the live collection
    reports: on 2026-11-06, before I0b adds pins, 194 node tests (the classification covers 178; `stream_cut.test.mjs` adds
    16, unclassified) + 40 pytest items = 234 items (derived), plus the adjacent
    `tests/test_serve_api.py::test_index_sets_security_headers`. The checker counts the live collection, never a number in
    prose. Target column format `path::test title`.
    `scripts/check_test_map.py` fails if an old test (node via the JSON reporter, pytest via `--collect-only`) has no row, a
    non-OBSOLETE target does not exist, or an OBSOLETE row lacks a cited reason; `[SEC]` rows are never OBSOLETE.
30. **R30 Copy and look.** The 13-case withdrawn-claims negative list, the header copy separating verbatim evidence from
    keyword-matched rules, the reliability note with its measured counts, and the agent hint and tracing-note wording
    (`tests/test_serve_agent_ui.py:89-97`) are ported character for character. Nothing moves. The old
    `test_nothing_in_the_new_ui_moves` (`tests/test_serve_agent_ui.py:77-79`) scans only the steps timeline's CSS for
    `transition`, `animation` and `transform`; the old drawer does slide (`index.html:33-34`: `transform` and
    `transition:transform .2s`). The new UI is stricter and has no motion anywhere, checked three ways: (1) a scan of the
    built CSS for declarations, not substrings (`transition:`, `transition-property`, `animation:`, `animation-name`,
    `@keyframes`), which cannot trip on a custom-property name and also proves no keyframes ship from `tw-animate-css` or
    `shadcn/tailwind.css`; (2) a Playwright check that the computed `transitionDuration` is 0s and `animationName` is `none`
    on every element of each page and state; shadcn animation classes are stripped; (3) the old `transform` substring check
    stays for the steps timeline and answer components, but not for layout utilities elsewhere: a static transform does not
    move, and (1) and (2) catch every transition or animation (inferred: Tailwind v4 uses `translate`/`transform` utilities
    for centring). The drawer therefore opens without the 0.2 s slide (D5). Colours follow `prefers-color-scheme`.

**State, storage, Turnstile**

31. **R31 Ask state and route changes.** Ask state lives in a provider above the router, so a route change neither aborts
    the stream nor loses the answer; the client aborts only on a deliberate user action or page unload, and shows a true
    status either way. An ask starts only from an event handler, never from an effect (React StrictMode re-runs effects, which
    would POST twice and spend twice). The POST is never retried.
32. **R32 Workspace storage.** The key `semigraph:workspace` in `sessionStorage` holds exactly `{id, token, expires_at}`
    (`index.html:589, 828-832`), so a workspace made on `/legacy` is restored on `/v2/` in the same tab and the reverse; every
    storage access is in try/catch; nothing is persisted elsewhere (no `localStorage`, no cookie, no URL).
33. **R33 Turnstile gate.** The server is the gate, as today, and stays fail-closed (`TURNSTILE_REQUIRED`). A public ask is
    answered from the cache BEFORE the bot check (`routes.py:343-346` against `:353-356`), so a client that held asks back
    for a token would block the cached examples that `MSG_PAUSED` and `MSG_RETRIEVAL_ONLY` promise still work (`:76, :86`),
    and would make the agent's own live check of a cached example impossible (M5_DECISIONS.md:241). So:
    (1) An ask is sent at once with the current token or none, as `index.html:665` does; the client never holds an ask
    back; a 403 gets the existing clause (`index.html:675`); workspace create is also sent at once (`:1043-1044`).
    (2) An upload waits up to 15 s for a token only when a site key is set and none is present (`index.html:1005-1008`; it
    applies to a first file too, and bites mostly the second and later files of a drop because the previous upload reset the
    widget), then fails with the old "bot check did not complete in time" row text; it never sends an empty header as if
    checked.
    (3) One widget lives in the app shell above the router, next to `AskProvider` (a route change does not re-render or
    re-challenge it). It is reset after each use and the token cleared, at the same four points as `resetTurnstile()`
    (`index.html:706, 675, 1019, 1050`); a StrictMode double mount leaves one live widget (the render is the one effect the
    page needs, R31 forbids effects only for requests); an `expired` callback clears the token. There is one token,
    consumed by whichever request uses it first, as on the old page: a workspace ask and an upload compete for it exactly
    as today, and a workspace ask with no token gets the server's 403 (workspace asks never touch the cache, `:339`).
    (4) `/api/config` failure: R8.
    Names unchanged: body `turnstile_token` for `/api/ask` and `POST /api/workspace`, header `X-Turnstile-Token` for
    uploads. The script is loaded from `https://challenges.cloudflare.com` only. Tests: a cached example clicked with no
    token sends exactly one POST and renders; an ask with no token reaches the server and a 403 shows the clause; an upload
    with no token waits, then fails as today; a route change does not re-render the widget; StrictMode leaves one live
    widget; expiry clears the token.
34. **R34 Workspace, upload queue and job watchers.** They live in a `WorkspaceProvider` above the router, next to
    `AskProvider`, because uploads spend the daily upload count and embedding CPU on a machine with a 3.2% memory margin
    (R13) and the server caps live job watchers at 3 (R19). A watcher starts only from the upload handler or an explicit
    restore, never from an effect, and is aborted when the provider tears down; at most one live watcher per job; the queue
    of a multi-file drop continues across a route change; a 429 on a watcher is retried per R19 but never by opening a
    second client watcher for the same job. Tests, under StrictMode with a route change in the middle: at most one live
    watcher per job (count the requests), exactly one upload POST per file, the queue continues.

## Security requirements and abuse cases (checkable)

- S1. No HTML sink in authored code: `web/src/**` has no `dangerouslySetInnerHTML`, `innerHTML`, `outerHTML`,
  `insertAdjacentHTML`, `document.write`, `eval`, `new Function`, string `setTimeout`, `srcdoc`,
  `createContextualFragment`, `DOMParser`, `window.open`, `location.assign`, `location.replace`, an assignment to
  `location.href`, or `setAttribute` of `on*`, `href` or `src`; the navigation and `setAttribute` forms are allowed only with a
  string-literal argument. (tests: oxlint with `react/no-danger` and the rules of R3, plus a pytest scan over all of
  `web/src/**`; the copied-in `components/ui/**` is scanned too, with no exclusion.) `package.json` carries no markdown or HTML renderer (`react-markdown`, `marked`,
  `markdown-it`, `dompurify`, `streamdown`, `rehype-*`, `remark-*`): a deny-list test. The built bundle is NOT grepped: the
  bundled react-dom contains these identifiers itself.
- S2. Rendered DOM allow-list: for every fuzz string (`<img onerror>`, `<script>`, `javascript:` and `data:` links, `]]>`,
  bidi and zero-width characters, unbalanced `**`, very long single lines (the length is a test parameter), `[` repeated,
  forged citation tokens, ids with quotes), each component has its own allow-list: the answer and what-changed text
  contain only `p`, `ul`, `li`, `h3`, `strong`, chip `button` and text; the evidence excerpt is text only; links are only
  the `safeUrl` anchor of S3 (the evidence metadata); company names and every `detail` string are text only. No `on*`,
  `src` or `style` attribute anywhere, and no `href` except on that anchor. The tokenizer runs in linear
  time on adversarial input (a timing test in the style of `test_cost_grows_linearly_with_length`).
- S3. Links: only structured URL fields become anchors, through `safeUrl`: `new URL()` parses, protocol is exactly `https:`,
  else the value is shown as text. Rejected cases tested: `javascript:`, `data:`, `vbscript:`, `blob:`, `mailto:`, `http:`,
  `//host`, mixed case, leading whitespace and control characters, `java\nscript:`, `https://user:pass@host`. Anchors carry
  `rel="noopener noreferrer"`. Answer, evidence and what-changed text is never linkified.
- S4. A chip id comes from untrusted model text: it is fetched only after an anchored match of the generated grammar
  (`ids.generated.ts`), and the URL is built with `encodeURIComponent`. Tests: `../`, `%2f`, `?x=`, trailing newline, a `doc:`
  id with no workspace open (no request is made). Route parameters get the same treatment (R25).
- S5. CSP and headers (gate 2). Today's string (`routes.py:69-71`, one line) is pinned as a literal in
  `tests/test_serve_ui_v2.py` and compared to every HTML response, not to `routes.CSP` (that would be tautological):

  ```
  default-src 'self'; script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src https://challenges.cloudflare.com; img-src 'self' data:; base-uri 'none'; form-action 'none'
  ```

  The four other headers of `SECURITY_HEADERS` are pinned too. The route walk cannot iterate `app.routes`: on FastAPI 0.141
  it holds `_IncludedRouter` wrappers, so a walk over it passes vacuously (`tests/test_serve_lifespan.py:542-544` says so and
  enumerates through `create_app().openapi()["paths"]`), and a Mount has no methods. The walk has three parts: (1) every
  GET path of `create_app().openapi()["paths"]` (`.openapi()` still builds the schema with `openapi_url=None`: read from fastapi 0.141.1 `applications.py:1070-1086`, the route alone is gated at `:1106`; not run), path parameters from a fixture table, a path with no table entry failing the
  test; (2) explicit probes for what the schema cannot list: `/v2/assets/x.js` and `/v2/assets/x.html` (the Mount),
  `/v2/index.html`, a `public/` file of the tmp dist, an extension-less deep link, the flipped root pages, and any
  `include_in_schema=False` route found by grep of `src/semigraph/serve`; (3) a non-vacuity assertion: the walk reached
  `/`, `/legacy`, one `/v2/` page, the assets mount and every GET path of `tests/test_serve_lifespan.py` `M4_PATHS` (the
  entries whose method set holds `get`; `/api/workspace`, `/api/workspace/{ws}/documents` and `/api/admin/freshness/check`
  are POST-only, `:531-538`, so a GET walk cannot reach them and the assertion does not claim them).
  Every response is fetched with the flags in each state of R7, every `text/html` response must be on an explicit allow-list
  (`/`, `/legacy`, the `/v2/` pages and deep links, the flipped root pages) and carry the literal, so an HTML route added
  later without the helper fails. Admin and token routes that answer JSON 401, 403 or 404 are fine. `/api/*` answers carry
  no new header. Today `test_index_sets_security_headers` (`tests/test_serve_api.py:203-208`) checks substrings only and
  stays. In a real browser (Playwright) every page and flow records zero `securitypolicyviolation` events and zero console
  errors, and every request goes to the page's own origin or `challenges.cloudflare.com` and nowhere else.
- S6. Untrusted stream data: every frame is parsed in try/catch and validated by type guards; text fields are rendered as text
  nodes only; numbers pass `Number.isFinite`; an unknown event name or extra field is ignored; `maxBufferSize` is set
  explicitly (R17: the parser's own default is unbounded) and a frame over it ends the stream as `failed`, never dropped
  silently; a malformed frame never throws past the client, including the parser's throw after a terminated state.
  (tests: fixtures plus mutated fixtures, including a frame that is not JSON, a `done` with the wrong field types and one
  frame over the limit.)
- S7. Turnstile is untouched server-side: no change to `guard.py` or the bot check (the verifier's `git diff --stat` shows
  none) and the existing Turnstile tests pass unmodified. Client behaviour is R33.
- S8. Workspace token: sent only in the `X-Workspace-Token` header; never in a URL, title, DOM text, console, `localStorage`
  or error text; the restore field is `type="password"` with `autocomplete="off"`; workspace routes already answer
  `Cache-Control: no-store`; unknown workspace and wrong token stay indistinguishable (404). (tests: DOM and console scans in
  the workspace Playwright flow; a unit test of the storage shape.)
- S9. Serving: the file lookup is contained (R9b); no directory listing; no root catch-all; `/api/*` never swallowed (R9d);
  `/api/config` exposes only its one key (R8); a missing dist can neither 500 nor stop boot (R13); no `.html` file leaves
  the file branch (R9c) and `html_response()` never mutates the shared header dict (R10). `security-reviewer` (opus) runs on
  `ui_v2.py`, because it does file I/O.
- S10. Supply chain (the owner's stage-4 rule): lockfile committed and created on Linux (R1); `npm ci --ignore-scripts`;
  `npm audit --audit-level=high` clean; `osv-scanner scan --lockfile web/package-lock.json` before every push that changes
  the lockfile; gitleaks; `semgrep` with JS/TS packs added to the merge-time command; a license report from the lockfile
  in `artifacts/m5b_licenses.json`, with two allow-lists: the shipped runtime bundle allows MIT, ISC, BSD and Apache-2.0;
  build and test tools also allow MPL-2.0 (`lightningcss` 1.33.0, a build-time dependency of `@tailwindcss/node` 4.3.4:
  npm registry, read 2026-11-06; and `axe-core`), and CC-BY-4.0 (`caniuse-lite`), CC0-1.0, BlueOak-1.0.0 and 0BSD if they
  appear (those four are inferred and settled from the lockfile at scaffold); the base image pinned by digest; a missing
  scanner is reported as not run, never as a pass. A test asserts the runtime stage has no Node (Dockerfile text pin), and
  the verifier runs `command -v node` in the built image when Docker is available.
- S11. Prompt-injection display: model answers, citations, evidence excerpts, uploaded-document text, company names, agent step
  summaries and `detail` strings are data. They are rendered as text only, never executed, never auto-fetched (only a chip
  click fetches, S4), never linkified; `doc:` evidence keeps its distinct "from your document" chip label
  (`docChipLabel`); nothing in them can change what a button does.
- S12. No paid call without a user action: loading any page issues no `POST /api/ask`, no upload and no workspace create; under
  StrictMode a submit sends exactly one POST (counted), an upload exactly one POST per file, and a job has at most one live
  watcher (R34); a route change or remount sends none.
- S13. The fixture server (`tests/e2e/serve_fixture_app.py`) never constructs `Settings()`: that class reads `.env` from the
  working directory (`config.py:142`) without exporting it, so a check of `os.environ` alone would pass with live provider
  keys and Neo4j credentials on disk. It builds the app from `FakeSettings` through `tests/serve_app_factory.py` (R16), the
  way `tests/test_serve_api.py:147-168` builds its own app, and patches all three answer paths to fakes:
  `routes.aanswer_stream`, `routes.astream_workspace_answer` (`routes.py:55, 57`) and the agent stream module stub
  (`test_serve_api.py:126-130`). Building from fakes is not enough on its own: request-time code still falls back to
  `get_settings()` lazily (`agent/stream_async.py:386`, `embeddings.py:106` and `:124`, `graph/client.py:92`), so a route
  left unfaked would read `.env` during a flow and pass a test that only starts the app. The server process therefore guards
  itself for its whole lifetime: first thing, before the app is built, it calls `install_guards()`, which replaces
  `Settings.__init__` (the class, so a reference to it is covered), `semigraph.config.get_settings`, `litellm.completion` and
  `litellm.acompletion` with raisers that also append to a hit list, and rewrites every loaded module attribute that is one
  of those originals, because the code binds them by name (for example `get_settings` at `embeddings.py:29`, `graph/client.py:19`,
  `agent/stream_async.py:116`, `agent/planner.py:24`, `llm.py:29`, `retrieval/answerer.py:44`, `retrieval/answerer_async.py:144`, `serve/main.py:31`, `serve/drain.py:109`; `completion` at `llm.py:26` and
  `retrieval/answerer.py:41`; `acompletion` at `retrieval/answerer_async.py:142`), and a patch of the defining module alone
  would miss them. Nothing undoes it in the server; every Playwright run is under it. The server never imports
  `semigraph.serve.main`: `app = create_app()` runs there at import (`main.py:456`) and `create_app()` calls
  `get_settings()` (`:446`). Tests: (1) in process, `install_guards()` returns an undo that the test calls in teardown, so
  other tests keep the real objects: after the install `Settings()`, `get_settings()`, a scan of `sys.modules`, after importing every module the server loads, that finds no attribute of any `semigraph.*` module that `is` one of the four originals and both
  litellm functions raise (the test then clears the hit list), and building the app leaves it empty; (2) in the same process, still under the guards,
  the API calls of every Playwright flow are driven once through `TestClient` and the hit list is empty at the end (the set of flows grows with each increment: at I1 it covers the routes the placeholder pages call; it is complete at CP-F2 and re-run at I4): a route
  that reached `get_settings()` or litellm fails the test, and one whose code swallowed the error (a broad `except`) shows in
  the list; (3) each refusal below. That replacing `__init__` on the pydantic `BaseSettings` subclass behaves as intended
  and that the by-name rewrite finds every binding is inferred, UNVERIFIED until I1. (A refusal to start when a `.env`
  exists was rejected: it would block every run from the repo root.) It also
  refuses to start when `ENVIRONMENT=production`, when `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` is present in its
  environment, or when asked to bind anything but 127.0.0.1; it reaches no Neo4j and no LLM. It is the real `ui_v2` router and the real routers wherever the existing fakes can answer, plus
  recorded JSON for the routes they cannot: `tests/agent_fakes.py`'s `FakeDriver` keys on the retriever's own query constants
  and does not define `FILINGS_QUERY` or `DOSSIER_ACTIVE_RISKS_QUERY` (grep, 2026-11-06), so the dossier, `/api/freshness`
  and the workspace job pipeline take recorded JSON and the recorded job stream; the Playwright upload flow replays the job
  stream, and the real parse and embed pipeline stays covered by the M4 tests and the owner's live check. Which route takes
  which source is settled in I1 and is UNVERIFIED until then. (tests: each refusal.)
- S14. Availability: the web build adds no resident memory (R13). The only v15 idle line on record is one boot (VmRSS
  2,136,884-2,139,932 kB, MemAvailable 1,635,640-1,652,748 kB, guard threshold 1,250,000 kB, M5_DECISIONS.md:240); its 3 MB
  range is a spread within one boot, not a boot-to-boot tolerance (inferred), so the baseline is a second reading of the
  running v15 machine, taken just before the preview deploy and compared with the idle line after it. The tolerance is
  fixed here, for the owner to change: a rise in VmRSS of more than 24,000 kB, about a third of the 77,444 kB peak margin
  measured in W1c (M5_DECISIONS.md:220), is a finding to explain before B6. Asset fan-out against Fly's `type = "requests"`
  limits (soft 20, hard 40, `fly.toml:62-63`) is unmeasured (inferred): the cold-load request count is recorded in
  `artifacts/m5b_page_weight.json`.
- S15. Public copy states strengths only (owner directive 2026-09-27): a scan of `web/src` copy for a banned-phrase list
  (the 13 withdrawn claims plus "1,000 users" and "1000 concurrent") finds none; runtime honesty that is part of the product
  (hedged wording, check badges) stays.
- S16. Diff scope: M5b touches only the files Appendix A names (it includes `fly.toml`, `[env]` flags only, and every
  other file the plan's own steps edit: the Appendix B documents, the root `README.md` paragraph, `tests/data/citation_corpus.json`,
  the vitest and Playwright configs and this spec's Status). The
  verifier's `git diff --stat` shows none of
  `src/semigraph/eval/agent_eval.py`, `guard.py`, `stream_runtime.py`, `retrieval/`, `state/`, `workspace_routes.py`,
  `dossier_routes.py`, any `.env*` file, and no change on `master`.

## Accessibility requirements (promotion gate 3, M5_DECISIONS.md:77, plus the SPA-specific items)

- A1. Citation chips are real `<button type="button">` elements with an accessible name, reachable by Tab, operable with Enter
  and Space, with a visible focus indicator, in the answer and in the what-changed view.
- A2. Focus returns to the right chip when the drawer closes, even after the answer re-rendered: a chip's key is its answer
  sequence + id + occurrence, so streaming appends keep it; when the opener is truly gone (a new answer replaced it), focus goes
  to the answer region's `tabindex="-1"` heading. (Playwright, both cases; this is the stale-opener case `208364a` left open.)
- A3. A polite live status region (`role="status"`) announces answer-ready, errors, a cut stream and terminal upload states; the
  streamed answer text is NOT a live region; the steps timeline keeps its `aria-live="polite"` (pinned today).
- A4. Every control has a programmatic label: the question box (visually hidden `<label for>`), the strategy select, the
  restore id and token inputs, the as-of date, the upload target select, the file input. Test:
  `web/e2e/a11y-labels.spec.ts` finds each control with `getByLabel` and runs axe's `label` rule.
- A5. The evidence drawer is a non-modal complementary landmark, `<aside aria-label="Evidence">`, the element the live page
  already has (`<aside id="drawer" inert aria-hidden="true">`, `index.html:120`) plus the missing name. Closed, it is
  `inert` and `aria-hidden`, so it is out of the tab order and the accessibility tree; on open focus moves to the named
  close button; Escape closes; the answer and every other chip stay operable while it is open (no focus trap, the page is
  not made inert); `openDrawer`/`closeDrawer` and the `openSeq` rule are ported (`index.html:747-760, 765`) with their
  tests. A modal dialog primitive is not used: `role=dialog` is not a landmark, and a modal traps focus and makes the page
  inert, which would turn today's non-modal drawer into a modal one and break A2's stale-opener case and the ported
  `openSeq` test. Recorded alternative, not chosen: Base UI Dialog with `modal={false}`, only if a smoke check shows it
  keeps the landmark semantics. Tests: axe and Playwright assert the `complementary` role named "Evidence" when open, and a
  second chip is clicked while the drawer is open.
- A6. The drop zone works from the keyboard (Enter and Space) and a real button reaches the file picker alongside drag and drop.
- A7. On a route change focus moves to the page's `h1` and `document.title` names the page; `header`, `nav` (named) and `main`
  landmarks exist. Test: `web/e2e/route-focus.spec.ts` (focus on `h1`, `document.title`, landmark roles, per page).
- A8. Both colour schemes pass the contrast audits; status is never conveyed by colour alone (badges carry text).
- A9. Nothing moves (R30); the layout is single column under 900 px as today (inventory 1.1).
- A10. Error and refusal text is announced through A3 and sits next to the control that caused it. Test:
  `web/e2e/error-placement.spec.ts` (each refusal of `refusals.json` lands in the A3 region and in the same container as
  its control).
- A11. One manual pass is recorded in `docs/v2/M5B_A11Y_MANUAL_PASS.md`: a keyboard-only walk of every flow and one
  screen-reader pass (NVDA with Chrome or Firefox on the owner's Windows 11 desktop, D15), by the owner or a person the
  owner names. Automated checks cannot detect all WCAG failures (Playwright's own accessibility page), so axe and
  Lighthouse do not replace it.

## Acceptance criteria (tests, written first; RED then GREEN evidence goes to Status)

- **pytest.** `tests/test_serve_ui_v2.py` (tmp dist fixture, no node): the state table (R7), the route walk and pinned literal
  (S5), `/api/nope` JSON 404 with flags on and off, deep link `/v2/company/NVDA/` 200 + `X-Robots-Tag: noindex`, missing asset
  404, traversal attempts (S9), `.html` probes (R9c), immutable asset header, `/api/config` key set, `/openapi.json` 404 (D16),
  `/` and `/legacy` without `X-Robots-Tag` in both request orders (R10), fail-safe on a missing dist directory and on a
  missing `index.html`, with `/api/config` still 200 (R13, R8), route order (a `/v2` page cannot shadow an API route), the flipped root pages and an unknown
  root path 404, redirect of the non-slash form from a fixed template (R9e). Also `tests/test_export_ids.py`,
  `tests/test_sse_fixtures.py`, `tests/test_dockerfile_web.py` (stage pins, no Node in the runtime stage),
  `tests/test_web_pins.py` (R1: exact versions, `.npmrc` flags, `engines`, lockfile Linux entries, the five Node versions
  agree), `tests/test_dist_contract.py` (R4), the web-source scan (S1), the fixture-server guards and refusals (S13),
  `scripts/check_test_map.py` (R29). Python coverage of new modules >= 80%.
- **Kept green until M6:** `tests/ui/*.test.mjs` (194 pass on Node v26.13.0, 2026-11-06, before I0b), `tests/test_static_ui.py`
  and `tests/test_serve_agent_ui.py` (40 items collected, before I0b), `tests/test_serve_api.py`. Not unchanged: I0b adds pins
  to `tests/ui/a11y.test.mjs` and `tests/test_static_ui.py` (I0b row, Appendix A), and worker A may move
  `tests/test_serve_api.py`'s app onto the shared factory (R16; `CACHED_DONE_BYTES` stays untouched).
- **vitest + Testing Library** (jsdom 30.1.3): the ported tests of `tests/ui` (chips, badges, checks, evidence, copy, a11y,
  agent, stream_cut, upload_flow, workspace), the S2 fuzz, the R21 differential test, the R33 Turnstile tests (a fake
  `window.turnstile`; StrictMode; remount; expiry; a cached example with no token sends one POST), the R31 single-POST
  test, the R34 watcher and upload-queue tests, the R23 and R25 tests. Coverage thresholds in `vitest.config.ts`:
  80% statements, branches, functions and lines over `web/src`, excluding only `components/ui/**` (copied-in shadcn) and
  `*.generated.ts`; the coverage globs are re-checked against vitest 5's new path semantics.
- **Playwright** (fixture server, Turnstile script stubbed at the network layer so the CSP is exercised for real; the real
  widget is checked by the owner in B6): deep link `/v2/company/NVDA/` with zero console errors and every asset 200 (B2/P6);
  the same `dist` at `/v2/` and flipped at `/` (P5, R12); keyboard flows for chips, drawer, drop zone (P3); Ask, escalation and
  agent flows over the recorded fixtures; workspace create, upload to `ready`, versions, as-of, what-changed; continuity: a
  workspace written by `/legacy` is read by `/v2/` and the reverse, and survives a route change (P6, R32); a route change
  mid-stream (R31) and mid-upload (R34); the origin and violation checks of S5. Navigation requests send
  `Accept: text/html`.
- **axe** (`@axe-core/playwright`, tags `wcag2a`, `wcag2aa`, `wcag21a`, `wcag21aa`, `wcag22aa`; the target is WCAG 2.2 AA):
  zero serious or critical on the five pages and on these states: drawer open, workspace open with a job, an error status,
  an answer with chips.
- **Lighthouse** (`lighthouse` CLI, fixture server): accessibility >= 95 and best-practices >= 90 on five pages
  (`/v2/`, `/v2/company/NVDA/`, `/v2/workspace/`, `/v2/freshness/`, `/v2/method/`) x mobile and desktop = ten runs. Performance
  and SEO are recorded, not gated (B3 sets no threshold). The S8 baseline (M5_PLAN.md:142; absent from
  `artifacts/m5_spikes.json`) is run on `/legacy` through the same server and recorded, not gated.
- **Gate 1:** `scripts/check_test_map.py` passes: every item the live collection reports (234 on 2026-11-06, before I0b) is
  mapped; zero UNCLEAR.
- **Live, by the agent, no paid call:** after the preview deploy: `/v2/` and a deep link answer 200 with the pinned headers,
  assets 200 and immutable, `/api/config` key, `/openapi.json` 404, a cached example streams and renders (it needs no
  Turnstile token, and R33 sends it without one), `/legacy` unchanged, `/` unchanged with `Cache-Control: no-cache` and no
  `X-Robots-Tag`, the memory line (S14), and the rollback drill (D18). A paid Ask needs a Turnstile token, which the agent
  session does not complete (M5_DECISIONS.md:241): the
  owner's first questions on `/v2/` are the paid check, and `/legacy` asking the same question is the control that separates
  an M5a fault from an M5b one.
- **Verifier checklist (Opus):** the pytest and web commands above; `grep` of `web/src` for the S1 identifiers (none); CSP
  literal across the route walk and the explicit probes (S5); `/api/nope` JSON 404 with flags on; `/openapi.json` 404;
  `docker build` succeeds and the runtime image has no `node`; the classification has zero UNCLEAR; `git diff --stat` per S16.

## Plan

Increments in order. Each one: tests first (RED seen), `ecc:code-reviewer` (`model: sonnet`) on each worker delivery,
`ecc:security-reviewer` (`model: opus`) where noted, the full suite, then the `verifier` (Opus) on the increment's diff.
Roles follow `rules/model-routing.md`: Sonnet main and workers, Opus verification. Workers (at most three, never more than two
at once here) get disjoint files and never touch `package.json`, the lockfile, `config.py`, `main.py`, `routes.py`,
`guard.py`, `store.py` or `ui_v2.py`; main creates a stub for every worker-owned module so imports resolve. A worker that
needs a new dependency stops and hands the need to main.

| Inc. | Owner | Content | Gate | Rollback |
|---|---|---|---|---|
| I0 | owner | Approve this spec (D1-D18). This plan was produced by `fable-architect` (Status, stage 1) and this revision applies an adversarial Opus review (Appendix C) | owner's written answer | n/a |
| I0b | worker, fast path | D9: the open a11y fixes on the live page (chips focusable with Enter/Space, `aria-live` on `#status`, a visually hidden label for `#q`, the stale-opener focus case, a named evidence landmark); D8's one-word copy fix ("13") if approved; pins in `tests/ui/a11y.test.mjs` and `test_static_ui.py`; CSP untouched. Committed in I0b, deployed with the I4 deploy on the owner's go (it edits `index.html`, the live page, so it has no deploy of its own) | the 194 + 40 stay green with the new pins; evidence in the commit body | revert the commit; after the deploy, the previous image (R7 commands) |
| I1 | main, serial | Scaffold and R2 smoke list; generators and tests (R5); `config.py`, `serve/ui_v2.py`, `/api/config`, `/legacy`, the `routes.index` delegate, `main.py` wiring; the fixture server (S13); Dockerfile web stage, `.dockerignore`, CI `web` job; placeholder pages; S8 baseline; `tests/test_serve_ui_v2.py`. The scaffold (lockfile created on Linux, `npx shadcn@4.23.0`, Playwright browsers) starts only after D17. Three commits so every intermediate image boots: server code (`config.py`, `serve/ui_v2.py` with `SPA_PAGES`, `/api/config`, `/legacy`, the `routes.index` delegate, `main.py` wiring) with flags off and no `web/`; generators with their committed outputs `web/src/lib/*.generated.ts` and tests; the `web/` scaffold with the Docker stage and CI together | D17 answered; smoke list recorded; R7-R13 tests green; CI green; `security-reviewer` (opus) on `ui_v2.py`; verifier | flags default false: today's surface; revert the commits |
| I2 | worker A, worker C (C1) | A: R15-R20 fixtures, recorder, byte test, `events.ts`, `sse.ts`, `jobStream.ts`, `M5A_SSE_CONTRACT.md`. C1: Company, Freshness, Method pages and their libs and flows (non-answer views start at once, M5_DECISIONS.md:90) | B1 restated passes (Python + vitest); contract doc matches the code (verifier); C1 pages pass axe; `security-reviewer` (opus) on the parser | revert; nothing is served until I4 |
| I3 | worker B, worker C (C2) | B: Ask page, `AnswerText`, badges, checks, chips, drawer, steps, `AskProvider`, vitest ports, fuzz, differential test, a11y, Playwright ask flows. C2: Workspace page, `WorkspaceProvider` (R34), `workspace/jobs/changes` libs, continuity flows. Main-owned in parallel: the Turnstile widget in the app shell and the `/api/config` client (R33, R8) | S1-S6, S8, S11, S12 tests; A1-A6; **checkpoint CP-F2** (D3); `security-reviewer` (opus) on the renderer, workspace and Turnstile code | revert; nothing is served until I4 |
| I4 | main | Classification refresh and `check_test_map.py`; Lighthouse and axe artifacts; `npm audit`, `osv-scanner`, gitleaks, `/security-review`, `spec-to-code-compliance` against this spec; Opus verifier on the phase diff; then, on the owner's go and with `check_env_fly.py` green and no gate window or demo running (M5_DECISIONS.md:61), the preview deploy: `UI_V2_PREVIEW = "true"` in `fly.toml [env]`, `check_env_fly`, `flyctl deploy --ha=false --remote-only --yes` (R7; old page at `/`, new at `/v2/`; the I0b fixes ride along); the agent's live checks above; then the rollback drill (D18): flag off on the same image, then on again, each deploy timed in `artifacts/m5b_rollback_drill.json`, `/v2/` 404 and `/` unchanged while off | all gates except B6/P8 recorded; deploy checks pass; drill recorded | `UI_V2_PREVIEW = "false"` and the same-image deploy of R7, or the v15 image `registry.fly.io/semigraph:m5a-d21-2afdd6e` (M5_DECISIONS.md:233) |
| I5 | owner, then main | B6: the owner's click-through and the manual pass (A11); named buyers' feedback (P8); the owner's written sign-off; then `UI_V2 = "true"` in `fly.toml [env]` (R7 commands); RUNBOOK and a strengths-only README paragraph (listed for the owner); stage 7 and 8 | the owner's written sign-off; P1-P9 recorded | `UI_V2 = "false"` and the same-image deploy of R7; `/legacy` stays through M6 |

Merge-time checks (`differential-review`, semgrep with JS/TS packs added, `spec-to-code-compliance` again) run when the
branch is ready to merge, as in the memory-guard precedent. The Neo4j stop-config deploy and the two deny-ruled file edits of
`docs/specs/m5a-followups.md` remain the owner's and do not block M5b.

## Cost

New recurring hosting, accounts, vendors and domains: $0 (M5_DECISIONS.md:110); no staging window; no Valkey. API spend
ceiling for M5b is $5 (B7, M5_PLAN.md:162; :112 calls the $5 ceilings unlikely to be reached). Expected spend is small and not
estimated here: the UI makes no paid call by itself, the paid checks are the owner's questions on `/v2/` (the ledger's typical
live answer is about $0.001-0.002, M5_DECISIONS.md:127), under the existing caps (150 asks and $10 a day, 20 and $1.32 per
address a day, 5 per 10 minutes, all unchanged and applying to the new page as to the old). Any scripted paid call carries
`--max-usd`; none is planned. The rollback drill (D18) is three deploys at $0 of API spend; each replaces and drains the one
machine (RUNBOOK.md:426-429), so the site is briefly unavailable on each. Image size delta and CI time are unmeasured; the
first is recorded in `artifacts/m5b_page_weight.json`.

## Owner decisions (recommendation first)

| # | Decision | Recommendation | Blocks |
|---|---|---|---|
| D1 | Approve this spec, including the gate map: B1 restated as the fixture and client gate (R15-R18), B2-B7 mapped to P1-P9, P5-P7 added, and B6's "preview path/staging" narrowed to the live `/v2/` path with no staging window (staging spend in package A covered S7 and S2, not M5b); P6 is not softened (D18) | Approve | Stage 2 |
| D2 | `/legacy` lifetime: P7 (kept through the M6 cutover) wins over B6 and M5_PLAN.md:218 (removed after a second confirmation) | P7 wins; removal becomes an M6 item | I4 |
| D3 | What "F2 fails a gate" means (never defined, M5_DECISIONS.md:155). At CP-F2 (end of I3), after one fix round reviewed by the verifier, F2 has failed if any of these cannot be met by a change inside `web/`: B1 fixtures and client tests; CSP byte-identical with zero violations on the five pages; one Turnstile widget in the app shell, reset after every use, one live widget under StrictMode and remount, asks never held for a token and an upload waiting at most 15 s (R33, as in the B4 row); the deep link 200 with zero console errors; B3 thresholds. A cause outside the framework (CSP, Turnstile, server) is not an F2 failure | Adopt this definition. On a failure main stops, writes the evidence and convenes `llm-council` before proposing the fallback order (Next.js static export with the same thin client, then the original choice); the owner decides | I3 |
| D4 | Base path: one artifact, absolute `base: '/v2/'`, runtime router basename, no `<base>`, no CSP change (R12). Alternatives: two builds with a full gate re-run on the promoted one, a server-side placeholder rewrite (risk: chunk paths baked into JS, inferred), a CSP change (needs the owner) | One artifact. Cheap and reversible; UNVERIFIED until the R12 test passes | I1 |
| D5 | `AnswerText` renders the old restricted grammar (headings, lists, bold, chips) rather than M5_PLAN.md:125's literal "plain text plus chips", which would drop constructs the live page shows (stack note finding 4) and so cut parity without the owner's approval. Two related looks: the evidence drawer stays non-modal, a complementary landmark like today's (A5); and the new UI has no motion anywhere, so the drawer opens without today's 0.2 s slide (R30; the old test covers only the steps timeline) | Port the grammar; non-modal landmark drawer; no motion | I3 |
| D6 | Header mechanism and SPA server: one `html_response()` helper, our own small router and a route-walk-plus-probes test (R9, R10, S5), rather than FastAPI 0.141.1's built-in `app.frontend()` or the pure-ASGI HTML header middleware of M5_PLAN.md:125 and M5A_BUILD_PLAN.md:106. Reason: every HTML response here is built by routes we own; `frontend()` has no header hook, prefers a `404.html`, raises at boot or answers 500 without its directory, and cannot serve enumerated root pages (R9 evaluation); a middleware inspects every response including SSE. Cost: no defence in depth against a future route that is added without a test update; a few dozen lines of our own code where a proven one exists, justified because the header control is the differentiator. Alternative: `frontend("/v2", ...)` plus a `/v2/`-only middleware | Own router and helper | I1 |
| D7 | Method page: name and content (M5_PLAN.md:22 says "Method & Limits"; the owner's strengths-only directive of 2026-09-27 governs public copy) | Name it "Method & Evidence", route `/method/`, runtime-honesty copy from already-pinned strings, any new sentence listed for your review; no limits list (those stay in internal docs) | I2 |
| D8 | Company set: pages for the 13 SEC filers (Samsung is in the 14-company `UNIVERSE`, has no SEC filings and its dossier 404s); the old line "SEC filings of 14 keystone companies" (`index.html:56`) is not pinned by any test (grep of `tests/`, 2026-11-06) but is public copy, and it overstates SEC coverage by one | v2 copy says 13 filers; fix the old page's line to say 13 in I0b (one word; public copy is yours to approve, and the never-overclaim rule points the same way) | I2 |
| D9 | The open a11y fixes on the live page first (I0b: chips, status live region, `#q` label, stale-opener focus, named evidence landmark). M5A_BUILD_PLAN.md:106 schedules them; M5_DECISIONS.md:142 covered only "the three accessibility gaps" that `208364a` already fixed (the other items went to the replacement page's gates), so the go for the rest is this answer. Skipping them would leave the live page, which visitors use until the flip, with mouse-only citations, and `/legacy` stays public through M6 | Do them (cheap, fast path); they deploy with I4, rollback is the previous image | I4 (the classification) |
| D10 | Flag names and meaning (`UI_V2_PREVIEW`, `UI_V2`, `/v2/`, `UI_DIST_DIR`), the flags living in `fly.toml [env]` (R7), the state table of R7, `/legacy` existing only while a flag is on, and `GET /api/config` as a new public route returning only the Turnstile site key (M5_PLAN.md:53, :125 and :210 name the flags and the route; none was answered individually) | Approve as written | I1 |
| D11 | Name the buyers who will preview `/v2/` (P8, package A item 8) | Needed before B6, not before the build; without names P8 stays open and `/` does not flip | I5 |
| D12 | `retrieval_only` in v2: keep the 503 "cached answers only" text; the example questions stay usable. A degraded stream needs a new route or a grammar change, and the grammar is frozen (M5_PLAN.md:94 tied it to the dropped `/api/chat`) | Keep the 503 | I3 |
| D13 | Stack pins: Node 24.22.2 (CI already runs 24; Node 26 became LTS 2026-10-28), TypeScript 7.0.2 with oxlint instead of ESLint, shadcn on Base UI (CSP provider; Radix is the alternative), `eventsource-parser` instead of porting the page's reader (maintained; its buffer limit is off by default, so R17 sets one), an own Turnstile hook instead of `@marsidev/react-turnstile` (R1), `@axe-core/playwright` (MPL-2.0, test-only); the pin list is completed from the lockfile (R1) | Approve | I1 |
| D14 | Who runs the paid checks and the B7 ceiling: the owner, in the browser (Turnstile), with $5 as the ceiling | Approve | I5 |
| D15 | Who does the manual accessibility pass and with which screen reader | The owner, or a person you name, with NVDA on Chrome or Firefox on the owner's Windows 11 desktop (Narrator with Edge as the fallback) | I5 |
| D16 | Close `/openapi.json`: Done in 568d7fa (fast path, outside M5b); confirm. M5_PLAN.md:33 and gate A4 (:160) promised it closed in M5a Step 0; it was served on live v15 and lists `/api/admin/*` (read from code, not checked live) | Yes | I1 |
| D17 | Approve downloading and executing third-party code for M5b, on this desktop and in CI: `npm install --ignore-scripts` of the R1 packages and the shadcn/Tailwind additions inside `node:24.22.2-slim` (lockfile creation), `npm ci --ignore-scripts`, `npx shadcn@4.23.0` init/add (it runs the CLI and its own install of component dependencies), and Playwright's Chromium download (`playwright install`). `ignore-scripts` stops lifecycle scripts only; it does not stop the shadcn CLI or the Playwright installer from executing code, which is why they need your go. The stack note held all of this back for it (line 10) | Approve with `ignore-scripts=true` pinned in `web/.npmrc` and the lockfile created on Linux | I1 |
| D18 | P6 as a test, not a promise. (a) Rollback: a live drill at I4 (preview on, then off on the same image, then on; each deploy timed; `/v2/` 404 and `/` unchanged while off), on your go for the three deploys. Alternative exception, if you would rather not take the outages: "paper drill plus the R7 state-table pytest", which waives the "tested" part of gate 6 and needs your explicit yes. (b) "In-flight workspace continuity" defined: the workspace, its documents and its stored `{id, token, expires_at}` survive a switch between `/legacy` and `/v2/` in the same tab; an upload in progress is not re-attached (the session key holds no job id and `GET /api/workspace/{ws}` returns documents and usage, no jobs, `workspace_routes.py:218-226`, `uploads/repo.py:215-228`), and its document appears when the job finishes. Persisting job ids would change the storage shape the legacy page reads and needs its own decision. Listed here as a limit of P6 | (a) live drill; (b) accept the definition and the limit | I4 |

None of D1-D18 is both ambiguous and expensive, so none meets the council rule today (D17 and D18 are permissions and a
definition, not open design questions). D4 and D6 are cheap and reversible before the flip; D3 sets the one trigger that would: if CP-F2 fails, the choice between repairing F2 and the fallback is
ambiguous and expensive, and the council is convened then, before any fallback work.

## Risks and where the obvious implementation is wrong

1. **`<base href>` injection** (M5A_BUILD_PLAN.md:106): the CSP's `base-uri 'none'` is expected to refuse it, and gate 2 forbids
   changing the CSP. Relative `./` assets 404 on deep links (both inferred). One artifact with an absolute base avoids both.
2. **`StaticFiles(html=True)` or `app.frontend()` as the SPA server**: Starlette's falls back to `404.html`, never
   `index.html`, and neither adds headers (they exist only in `GET /` at `routes.py:171-175`); FastAPI's `frontend()` adds
   the `index.html` fallback but still no header hook (R9).
3. **A root catch-all**: it swallows `/api/*` and later routes (`/mcp`). Root pages are enumerated; `/v2/` is the only prefix
   with a fallback.
4. **Route order and `GET /`**: `routes.router` registers `GET /` before `ui_v2` can, so the flip is a delegate inside
   `routes.index`, not a second `GET /`.
5. **Plain-text `AnswerText`** regresses headings, lists and bold (D5).
6. **Double effect, double spend**: StrictMode re-runs effects; an ask, an upload or a job watcher started from an effect
   posts or connects twice, and the server caps watchers at 3 per job; a Turnstile render started from an effect renders
   twice (R31, R33, R34, S12).
7. **A route change mid-stream** drops the connection and the answer; the old page never navigated (R31).
8. **Fonts**: Vite inlines assets under 4 KiB as `data:`; the CSP has no `font-src` (R4).
9. **Tool churn**: TS 7 has no programmatic API until 7.1; shadcn's docs still say `baseUrl`; jsdom 31 shipped 2026-11-05
   (take 30.x); vitest 5 changed coverage path semantics; several tools need Node >= 22.19 or 22.22.
10. **Memory margin is 3.2%**: nothing from the web build may be resident; asset serving uses `FileResponse` and the default
    thread pool briefly (inferred; unmeasured), and a SPA's request fan-out counts against Fly's soft 20 / hard 40 (R13, S14).
11. **Stale test counts**: the classification says 178 and the decision files say 149; the runner says 194 today. Gate 1 uses
    the live count and a machine check, not prose.
12. **Turnstile is stubbed in automated tests** (the CSP is real, the widget is not): the real widget is first exercised by the
    owner in B6, and under React double-mount it is unverified until R33's tests run. Holding an ask back for a token would
    block the cached examples, which the server answers before the bot check (R33).
13. **M5a leftovers**: the first live paid question is also the first live check of v15's paid path (M5_DECISIONS.md:241);
    `/legacy` is the control. `/openapi.json` was an unmet M5a gate A4 item (D16): closed in code by 568d7fa, live at I4.
14. **Documents already stale** (Appendix B): reading M5_PLAN 4.6-4.7 or B1 literally would rebuild the encoder.
15. **Gate-2 bypasses the obvious code has**: the extension branch serving `/v2/index.html` without the CSP, `html_response()`
    mutating the shared `SECURITY_HEADERS` dict (noindex on the live `/`), and a route walk over `app.routes` that sees
    neither the Mount nor, on FastAPI 0.141, the included routes (R9c, R10, S5).
16. **eventsource-parser is unbounded by default** (`maxBufferSize` is `undefined`): a limit must be set and its
    overflow handled as a failure (R17).

## Appendix A: files M5b may create or change

Main: `web/` shared surfaces (`package.json`, lockfile, `.node-version`, `.npmrc`, `vite.config.ts`, `tsconfig*.json`,
`vitest.config.ts`, `playwright.config.ts`, the Lighthouse config, oxlint config, `index.html`, `src/{main,App,routes}.tsx`, `src/index.css`, `src/components/ui/**`,
`src/components/TurnstileWidget*`, `src/lib/{config,safeUrl,turnstile}*`, generated files, stubs), `config.py`, `main.py`,
`routes.py` (`index` only), `serve/ui_v2.py`, `fly.toml` (the `[env]` flags `UI_V2_PREVIEW` and `UI_V2` only, at I4 and I5),
`scripts/export_ids.py`, `scripts/check_test_map.py`, `Dockerfile`, `.dockerignore`, `.github/workflows/tests.yml` (the new
`web` job and the two existing `node-version` lines),
`web/.gitignore` (`node_modules/`, `coverage/`, `test-results/`, `playwright-report/`, the Lighthouse output directory; the root `.gitignore:5` `dist/` already covers `web/dist`), `web/public/**`, `src/pages/NotFound*`, `src/components/{Layout,Nav}*`, the vitest setup file, `tests/test_serve_ui_v2.py`, `tests/serve_app_factory.py` (created in I1 with the fixture server; worker A uses it and hands any change it needs to main, as with dependencies), `tests/test_export_ids.py`, `tests/test_dockerfile_web.py`, `tests/test_web_pins.py`,
`tests/test_dist_contract.py`, `tests/e2e/serve_fixture_app.py` (and its recorded JSON),
`tests/data/citation_corpus.json` (R5), `web/e2e/{deeplink,continuity,a11y-labels,route-focus,error-placement}.spec.ts` and the
CSP/origin and Turnstile specs of Acceptance (file names settled in I1), the Lighthouse and axe runners, `docs/v2/M5B_TEST_CLASSIFICATION.md`,
the other Appendix B documents it amends in I1 (`docs/v2/M5_PLAN.md`, `docs/v2/M5_DECISIONS.md`, `docs/v2/M5A_BUILD_PLAN.md`,
`docs/v2/PLAN.md`), `docs/RUNBOOK.md`, `web/README.md`, the root `README.md` (the I5 strengths-only paragraph, listed for the
owner), `docs/v2/M5B_A11Y_MANUAL_PASS.md`, `artifacts/m5b_*.json`, and this spec (`docs/specs/m5b-frontend.md`: its Status
and Appendix C only). Worker A: `web/src/lib/{events,sse,jobStream}.ts` and tests,
`tests/data/sse/*`, `tests/test_sse_fixtures.py` (and, if the worker chooses, the fixture of
`tests/test_serve_api.py`), `docs/v2/M5A_SSE_CONTRACT.md`. Worker B:
`web/src/pages/Ask*`, `web/src/components/{AnswerText,Badges,Checks,Chips,EvidenceDrawer,Steps}*`,
`web/src/state/AskProvider.tsx`, their tests, fuzz fixtures and the differential test, `web/e2e/ask*.spec.ts`. Worker C:
`web/src/pages/{Company,Freshness,Method,Workspace}*`, `web/src/state/WorkspaceProvider.tsx`,
`web/src/lib/{company,workspace,jobs,changes}.ts`, their tests,
`web/e2e/{company,freshness,method,workspace}*.spec.ts`. I0b worker: `serve/static/index.html`, `tests/ui/a11y.test.mjs`,
`tests/test_static_ui.py`. Owner: `.env.example` lines.

## Appendix B: statements this spec supersedes (amended in I1; none edited by this draft)

| Document | Statement | Now |
|---|---|---|
| `M5_PLAN.md` | :22, :125 Next.js static export, AI SDK, `generateStaticParams`, AI Elements, Node 22, `/_next/static/*` | F2 (decision 10); Vite assets under `/v2/assets/`; Node 24.22.2 |
| `M5_PLAN.md` | :94, :119-121 `/api/chat`, eight AI-stream fixtures, `tests/test_serve_ai_stream.py` | void with the encoder; R15-R18 |
| `M5_PLAN.md` | :125 "middleware on every HTML response", "plain text nodes plus chips" | helper (D6); restricted grammar (D5) |
| `M5_PLAN.md` | :137 S3 AI SDK probe; :162 B1; :170 Step 0 worker A (encoder); :218 second B6 confirmation | void; restated; encoder dropped; P7 wins (D2) |
| `M5_PLAN.md` | :36 CSP at `routes.py:34-39` | `routes.py:69-74` |
| `M5_DECISIONS.md` | :75, :99 framework pending | answered at :155; '149 node tests' (:75-76, :90, :142) is 194 today |
| `M5A_BUILD_PLAN.md` | :106 injected `<base href>`, router basename from `document.baseURI`, "pure-ASGI header middleware"; chip lines 277/548/745 | R12 and D6; chips are at `index.html:278` and `:549` |
| `docs/v2/PLAN.md` | :12, :40-41 Next.js static + AI SDK on Cloudflare Pages; M3 "AI SDK stream encoder"; section 13 "AI SDK protocol churn" | F2, same origin, no encoder |
| `M5B_TEST_CLASSIFICATION.md` | HEAD 2ed0a40, 178 tests, 218 items, omits `stream_cut` | refreshed in I4 (R29) |
| `M5_PLAN.md` | :33 `/openapi.json` "Closed in M5a Step 0"; :160 A4 `/openapi.json` 404 | closed in code by 568d7fa, live at I4 (D16) |

## Appendix C: review disposition

An adversarial Opus review of the first draft found 10 major and 10 minor problems. IDs: M1-M10 in the order of the
review, m1-m10 likewise; E1-E4 are things found while applying it. "Applied" means the review's fix as written.

| ID | Finding | Done | Where |
|---|---|---|---|
| M1 | R33 holds asks for a Turnstile token, which blocks cached examples (the server answers them before the bot check) and the agent's own live check | Applied, with one difference: the review said only the second and later file of a drop waits; the old code waits for any upload that has no token (`index.html:1005-1008`), so R33 ports that. Round 2 found D3 still listing the held-ask behaviour as an F2 failure criterion: fixed (R2-1) | R33, R8, Acceptance (live), Risk 12, D3 (R2-1) |
| M2 | Gate-2 holes: the extension branch serves `/v2/index.html` without the CSP; the helper may mutate the shared header dict; redirects from the raw path; the walk cannot see Mounts | Applied. The walk needed more than the review said (E2) | R9c, R9e, R10, S5, S9, Risk 15 |
| M3 | The fixture server checks only `os.environ`; `Settings()` reads `.env` | Applied differently: never construct `Settings()` (test patches it to raise), `FakeSettings` only, all three answer paths patched, a litellm-raises test. Not applied: refusing to start when a `.env` exists, because `.env` is in the repo root (listed by name, not opened) and the refusal would block every run from there. Round 2: the safeguards were tests only and request-time code still falls back to `get_settings()`; the server process now guards itself for its lifetime (R2-5) | S13 |
| M4 | "Only install command is `npm ci`" cannot hold at scaffold; Windows lockfile; no decision for executing packages | Applied: `.npmrc` pinned, lockfile created on Linux, a test for the Linux entries (the Windows risk is inferred), D17 added | R1, D17, I1 |
| M5 | A5 asks for a landmark and a modal dialog at once | Applied: non-modal `<aside aria-label="Evidence">` recommended and written down; Base UI `modal={false}` recorded as the unchosen alternative | A5, R24, D5 |
| M6 | P6 softened without a decision: paper rollback drill; "in-flight" undefined | Applied: live drill is the recommendation, the paper exception is an explicit alternative, "in-flight" defined and its limit listed | D18, gate map P6, I4, Cost |
| M7 | No lifecycle for uploads and job watchers under StrictMode and route changes | Applied | R34, R19, S12, Risk 6 |
| M8 | Flags' home and `fly.toml` missing from Appendix A; rollback has no command | Applied: `fly.toml [env]`, commands cited from RUNBOOK.md:26, :134, :139. That `--image` applies an edited `[env]` is inferred; the drill checks it | R7, Appendix A, I4, I5, S16 |
| M9 | R4's "no absolute URL in dist" fails on react-dom | Applied (re-read from jsDelivr); every R4 item gets a named post-build test | R4, R14, Acceptance |
| M10 | FastAPI 0.141.1 `app.frontend()` never evaluated; "no SPA fallback" statement | Applied differently: evaluation recorded from the installed source; the Starlette statement corrected (it falls back to `404.html`); recommendation is to keep our router, not use `frontend()`, for the reasons in R9 and D6 (the review allowed either) | Why, R9, D6, Risk 2 |
| m1 | R13's fail-safe would crash boot | Applied | R13, S9 |
| m2 | Agent clock makes the byte test non-deterministic; source of `.expected.json`; buffer default | Applied for the clock (patch `_now`) and `.expected.json` (from Python event dicts). The buffer default was read now instead of at scaffold: it is unbounded (E1) | R15, R16, R17, S6 |
| m3 | Python `\Z` does not translate to JS; classifiers not cross-tested | Applied | R5 |
| m4 | shadcn/Tailwind dependencies and licenses unaccounted; the port drops the `transform` check | Applied for the list and licenses (shadcn's `tw-animate-css` read from the shadcn registry, lightningcss MPL-2.0 from the npm registry). Applied differently for `transform`: kept for the steps timeline and answer components, not globally, with the reason; E4 | R1, R30, S10 |
| m5 | Criteria without tests; axe tags; S2 allow-list; S14 tolerance | Applied for tests, tags and allow-lists. S14 applied differently: only one v15 idle line exists (M5_DECISIONS.md:240), so the baseline is a second reading before the deploy and the bound is fixed at 24,000 kB, justified from the W1c margin | R1, R23, A4, A7, A10, S2, S14, Acceptance |
| m6 | Contradictions: three vs four config keys, "exactly today's surface", hard-coded 234 | Applied differently: `/api/config` drops the two flags (the review's second option), so it is one key everywhere; the other two fixed as written | R7, R8, S9, D10, R29, Acceptance |
| m7 | CI and config wiring: Python setup, Node agreement, site key, config failure | Applied: the dummy key for the Turnstile specs is a plain string; no Cloudflare test key is named because none was fetched | R14, R1, R8 |
| m8 | Extra XSS sinks; route parameter validation | Applied | S1, S4, R25 |
| m9 | Three inaccurate claims (memory figures, D9's authority, `/openapi.json`) | Applied; `/openapi.json` is recorded as a failed M5a gate in Status | R13, D9, D16, Status |
| m10 | D8, D15, Turnstile wrapper, I0b timing, fable-architect | Applied: 13 recommended in I0b, NVDA recommended, own hook, I0b deploys with I4, Status says the plan came from `fable-architect` (per the orchestrator) instead of "review pending" | D8, D15, R1, I0b, Status |
| E1 | eventsource-parser 4.1.3 has no default `maxBufferSize` (`@defaultValue undefined (unbounded)`); R17, S6 and D13 said limits stay on | Fixed: explicit limit, overflow is `failed`, terminated-parser throw caught, `done_max_size.sse` | R15, R17, S6, D13, Risk 16 |
| E2 | The S5 walk over `app.routes` is vacuous on FastAPI 0.141 (`tests/test_serve_lifespan.py:542-544`) | Fixed: OpenAPI-path walk plus probes plus a non-vacuity assertion | S5 |
| E3 | `index.html:820-823` cited for the Turnstile block; it is `:821-824` | Fixed | R1, R8 |
| E4 | The old motion test scopes to the steps timeline; the old drawer slides 0.2 s | Stated; the new UI has no motion anywhere, listed for the owner | R30, D5 |

**Round 2.** A second Opus re-review of the revision above reported one unaddressed finding (the remainder of M1) and six
new issues: one major, which is that same remainder, and five minor, so six distinct fixes. IDs R2-1 to R2-6 in the order of
the review; each fix was re-checked against the repo (file:line in the Done column where the fix rests on code).

| ID | Finding | Done | Where |
|---|---|---|---|
| R2-1 | major, and the unaddressed rest of M1: D3, the text the owner signs to define when F2 has failed, still lists "a fresh Turnstile token per ask and upload" as a criterion, which a correct R33 build fails (R33(1) sends an ask at once with the current token or none) | Applied: D3's clause now reads one widget in the app shell, reset after every use, one live widget under StrictMode and remount, asks never held for a token and an upload waiting at most 15 s (R33). The review's replacement text and the B4 row's words differ slightly; the clause takes the B4 row's wording and adds the review's "never held, 15 s" half | D3 |
| R2-2 | minor: S16 limits the diff to Appendix A, but I1 amends the Appendix B documents, I5 adds a README paragraph, R5 creates `tests/data/citation_corpus.json`, the vitest and Playwright configs and this spec's Status are not listed | Applied by adding the files to Appendix A (the review's first option; S16 now names the groups). Beyond the review's list: the CSP/origin and Turnstile Playwright specs that Acceptance requires and no listed glob covers, with their names left to I1 | S16, Appendix A |
| R2-3 | minor: S5's non-vacuity assertion requires every `M4_PATHS` path, but three entries are POST-only (`/api/workspace`, `/api/workspace/{ws}/documents`, `/api/admin/freshness/check`, `tests/test_serve_lifespan.py:531-538`) and a GET walk cannot reach them | Applied: "every GET path of `M4_PATHS`", with the three POST-only entries named and stated as not claimed | S5 |
| R2-4 | minor: R8 and R7 promise `GET /api/config` in every state but R13 registers "the v2 routes" only when the dist has `index.html`, and the spec never says which side `/api/config` is on | Applied: registered unconditionally on its own router in `ui_v2.py`, outside the R13 check, with the missing-dist and missing-`index.html` tests (and the Acceptance line) asserting 200 with the one key. A grep for `api/config` found no route of that name today | R8, R13, Acceptance |
| R2-5 | minor: S13's Settings and litellm safeguards are tests only; request-time code falls back to `get_settings()` lazily (`agent/stream_async.py:386`, `embeddings.py:106, :124`, `graph/client.py:92`), so an unfaked route would read `.env` and pass both tests | Applied differently: same goal and lifetime (the fixture-server process replaces `Settings`, `get_settings`, `litellm.completion` and `acompletion` with raisers for its whole life, the flow-driving test runs under them), but the replacement is at class level plus a rewrite of every by-name binding, because the code does `from ..config import get_settings` (`embeddings.py:29`, `graph/client.py:19`, `agent/stream_async.py:116`) and `from litellm import completion` / `acompletion` (`llm.py:26`, `retrieval/answerer.py:41`, `retrieval/answerer_async.py:142`), and a patch of the defining module alone leaves those bindings live. Added a hit list so a swallowed error still fails, and the server never imports `serve.main` (`app = create_app()` calls `get_settings()` at import, `main.py:446, :456`). The mechanism is inferred, UNVERIFIED until I1 | S13, Acceptance |
| R2-6 | minor: Acceptance calls `tests/ui/*.test.mjs` and `tests/test_static_ui.py` "unchanged", but I0b adds pins to `a11y.test.mjs` and `test_static_ui.py` | Applied ("Kept green until M6", the I0b pins stated). One addition: `tests/test_serve_api.py` is also not strictly unchanged (R16 lets worker A move its app onto the shared factory), so it is stated in the same sentence | Acceptance |

**Round 3.** A final Opus check (2026-11-06, verdict READY_FOR_OWNER) left seven minor wording fixes, all applied as the check worded them (the `openapi_url=None` fix is committed as 568d7fa, so the committed branch of leftover 1 was used).

| ID | Leftover | Done |
|---|---|---|
| R3-1 | `/openapi.json` text still said served and D16 open | Applied: intro, Scope 9, I1, D16, Risk 13, Appendix B row and the Status A4 row now say closed in code by 568d7fa, live at I4; the three Acceptance 404 checks stay as regressions |
| R3-2 | I1 commit order contradicted itself (generators before `ui_v2.py`) | Applied: server code first, then generators with committed outputs, then the `web/` scaffold |
| R3-3 | `tests/serve_app_factory.py` owned by worker A but needed by main in I1 | Applied: moved to main's list (Appendix A), R16 says (main, I1), S13 test (2) says the flow set grows per increment |
| R3-4 | S13's by-name `get_settings` list incomplete; test (1) checked only that list | Applied: list reworded as examples with the missing bindings; test (1) is a `sys.modules` scan |
| R3-5 | Appendix A did not name the scaffold's own files (`web/.gitignore`, `web/public/**`, NotFound, Layout/Nav, vitest setup) | Applied: added to main's `web/` list (the review's first option) |
| R3-6 | Status stage-1 row said the round-2 fixes were not re-reviewed | Applied: replaced, and the row records the final Opus check |
| R3-7 | S5 marked `.openapi()` behaviour UNVERIFIED; round-2 intro overstated file:line coverage | Applied: S5 cites fastapi 0.141.1 `applications.py:1070-1086`, `:1106`; Appendix C intro says "where the fix rests on code" |

## Status

| Stage | Gate | Evidence |
|---|---|---|
| 1 Spec | owner approves; plan via `fable-architect` | **APPROVED by the owner 2026-11-06**: every decision D1-D18 answered with its recommendation (D17 downloads and executes the listed npm packages, the shadcn CLI and Playwright's browser with install scripts off and a Linux-built lockfile; D18a the live rollback drill at I4). Done: the plan was produced by the `fable-architect` agent (workflow run `wf_cfa13512-55f`, agent "M5b plan (architect)", per the orchestrator's record; the run was not re-read for this revision). DRAFT written 2026-11-06 and revised the same day after an adversarial Opus review and a second Opus re-review of that revision (Appendix C, round 2). The round-2 fixes were re-checked by a final Opus pass on 2026-11-06: all six addressed, verdict READY_FOR_OWNER, minor leftovers fixed or listed. The spec passed that final Opus check on 2026-11-06 (READY_FOR_OWNER) with its minor leftovers applied (Appendix C, round 3). Nothing was run to revise it beyond reads of the repo, the installed Python sources and public registry and source pages |
| M5a gate A4, `/openapi.json` 404 | M5_PLAN.md:160 | PASS in code (568d7fa), not checked live; live after the I4 deploy |
| I0b live-page a11y fixes (D9) and D8 copy | fast path, TDD; the 194 + 40 stay green with the new pins; CSP untouched; committed in I0b, deployed with I4 | **GREEN in the working tree 2026-11-06, not committed, not deployed, not yet reviewed (`ecc:code-reviewer`, Opus verifier pending).** Files: `src/semigraph/serve/static/index.html`, `tests/ui/a11y.test.mjs`, `tests/test_static_ui.py` (`tests/ui/harness.mjs`, `tests/test_serve_agent_ui.py`, `routes.py` untouched; `git diff --stat src/semigraph/serve/routes.py` empty, so the CSP is byte-identical; no `<script>`, `style=` attribute or origin added). Done: chips are `role=button tabindex=0` spans from one helper (`chipHtml`), Enter on keydown and Space on keyup (see below), `.cite:focus-visible` ring; `#status` is `role=status aria-live=polite`; visually hidden `<label for=q class=sr-only>Your question</label>`; `<aside id=drawer aria-label="Evidence">`; stale opener: focus returns to the same chip (container, id, occurrence, `citeGen`) in the re-rendered text, else to `h2#askHeading tabindex=-1`, never `<body>`; D8: "SEC filings of 13 keystone companies". **RED** (tests only, before any page edit): `node --test tests/ui/*.test.mjs` 240 tests, 218 pass, 22 fail; `pytest tests/test_static_ui.py tests/test_serve_agent_ui.py tests/test_serve_api.py` 134 collected, 8 failed (7 new pins + `test_node_unit_tests_pass`, which wraps the node suite), 126 passed. **GREEN**: node 240/240 (194 + 46 new); pytest 135 passed (127 + 8 new pins: the 7 of the RED run plus a glued-attribute pin added after it, whose own RED was the re-introduced defect, 1 failed, then 1 passed once fixed; the two pin files collect 48 items, was 40). No old test was deleted or weakened (240 = 194 + 46 new). Old `a11y.test.mjs` tests were edited only in their setup: `chip()` now builds a chip from the page's own renderer output, `#answer` and `#wsChanges` are `Container` fakes, one test was renamed ("with an opener that cannot hold focus ...") and the what-changed test takes its chip from `changeItemHtml`. Mutation check: 19 single-point breaks of `index.html` (Space on keydown, no `citeGen` bump, no heading fallback, occurrence ignored, no role/aria-live, chip not focusable, keydown not consumed, repeat allowed, stale never detected, no landmark name, label hidden by `display:none`, no label, copy 14, no keyup/keydown handler on a container, answer made live, no focus ring, attribute order) all caught; an attribute glued to the one before it (found in review of the diff: `id="q"maxlength`) is now pinned too. Real Chrome (Browser pane, page loaded as a `data:` URL, no API): Space and Enter on a focused chip open the drawer and focus its close button, the drawer stays open, the page does not scroll, Escape returns focus to the chip; a streaming-style re-render of `#answer` gives focus back to the same chip, a new `ask()` gives it to `#askHeading`; the label is in the accessibility tree. **Not done / deviations:** (1) Space activates on keyup, not on keydown like the drop zone, because the drawer takes focus at once and some browsers then deliver the keyup to the close button; Firefox and Safari were not tried. (2) A chip is a `role=button` span (as M5A_BUILD_PLAN.md section 8 says), not a native `<button>` (A1 asks for that in the SPA). (3) The what-changed chips fall back to the Ask heading, not the workspace heading. (4) Other controls still lack labels (strategy select has `title` only; restore id and token inputs have placeholders only; as-of date has `title`; the hidden file input has none): A4 in the SPA, not in the I0b list. (5) `README.md` lines 6 and 357 still say "14 keystone" (public copy, outside I0b's files). **Review fix (MEDIUM, one global `citeGen` shared by `#answer` and `#wsChanges`, 2026-11-06):** a version compare (`showChanges`) bumped the count the remembered place of an `#answer` opener is checked against, so after the next streaming re-render, focus went to `#askHeading` although the chip was on screen (A2). Now one count per container (`citeGens = { answer, wsChanges }`): `ask()` bumps only `answer`, `showChanges` and the first-version branch only `wsChanges`; `chipPlace` stores the container key and `chipAt` compares against that container's count (`index.html` only; no test file other than `a11y.test.mjs` touched). **RED** (two tests added to `tests/ui/a11y.test.mjs` before the page edit: Enter on an `#answer` chip, `showChanges`, re-render `#answer`, close; and the reverse, Enter on a `#wsChanges` chip, `ask()`, re-render `#wsChanges`, close): `node --test tests/ui/a11y.test.mjs` 75 tests, 73 pass, 2 fail (focus on `#askHeading`). **GREEN**: same command 75/75; `node --test tests/ui/*.test.mjs` 242/242 (240 + 2); `pytest tests/test_static_ui.py tests/test_serve_agent_ui.py` 48 passed. Mutation check on a scratch copy (5 single-point breaks: all bumps on one count, `ask()` bumping `wsChanges`, no bump in `ask()`, no bump in `showChanges`, `chipAt` always comparing the `answer` count): all caught (the first, second and last by the two new tests; the two "no bump" ones by the existing "new answer" and "new report" tests) **Reviewed and verified (2026-11-06):** `ecc:code-reviewer` APPROVE with 1 MEDIUM fixed (one `citeGen` shared by `#answer` and `#wsChanges`; now one count per container, `citeGens`); Opus verifier PASS WITH CONDITIONS (mutation runs on a scratch copy; suites green: node 242 then 243 with the escaping test below, pytest 135 on the pin and API files; CSP byte-identical: `routes.py` unchanged, no `<script>`/`style=`/origin added). Conditions closed: W1, a node test that a hostile `chunk_id` in the what-changed view stays text (fails with `esc()` removed from `chipHtml`, passes restored). **Known limit (W2):** chips are now Tab stops and every streamed delta re-renders `#answer`, so focus on a chip during streaming falls to `<body>` on the next delta; the v2 page (I3) re-renders without replacing focused nodes. Follow-up for the owner: `README.md:6`, `README.md:357` and `docs/KNOWN_LIMITS.md:7` still say "14 keystone" (public copy; not changed without the owner). |
| 2 Dev (TDD) | tests red then green; reviews clean | pending |
| 3 Regression | full suite green, coverage >= 80%, evals at threshold | pending (pytest, vitest, Playwright, axe, Lighthouse B3) |
| 4 Security | `/security-review`, gitleaks, `npm audit`, `osv-scanner` before push; `differential-review`, semgrep, `spec-to-code-compliance` at merge | pending |
| 5 Fix findings | only if stage 4 finds any | pending |
| 6 Re-test and verifier | stage 3 and 4 again on the changes; Opus verifier | pending |
| 7 Finalize and push | conventional commits, CI green (new `web` job and both Python selections), preview deploy, B6 written sign-off, P8, flip | pending |
| 8 Archify | expected to apply (a new `web/` module boundary, a new Docker stage and a new dependency tree); offline view from the repo, online view from the Dockerfile and `fly.toml` only | pending |

Gate tracker: B1-B7 and P1-P9 are tracked in the gate map above; every one is pending, and its evidence is added to the
stage rows as it exists.
