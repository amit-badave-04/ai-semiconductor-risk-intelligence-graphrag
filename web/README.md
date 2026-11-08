# web/: the single-page app

Vite + React + TypeScript, Tailwind v4, shadcn components on Base UI, React Router (declarative mode). Same-origin with the API,
served by the FastAPI app from `/v2/` (and, once `UI_V2` flips the root, from `/`). Specification: `docs/specs/m5b-frontend.md`.

One build serves both paths: the Vite `base` is the absolute `/v2/` and the router basename is chosen at run time from the URL
(`src/lib/basename.ts`). The server's Content-Security-Policy is unchanged, so the build has no inline script or style, no
`<base>`, no web font and no motion; `tests/test_dist_contract.py` checks that on every build.

## Set up (exact commands)

Node is pinned to **24.22.2** (`.node-version`; `engines.node` is the floor `>=24.22.2`; `.npmrc` makes npm enforce it).

```bash
# 1. Node 24.22.2 (Linux x64 shown; any install of exactly this version works, nvm/fnm read .node-version)
curl -fsSLO https://nodejs.org/dist/v24.22.2/node-v24.22.2-linux-x64.tar.xz
curl -fsSLO https://nodejs.org/dist/v24.22.2/SHASUMS256.txt
grep " node-v24.22.2-linux-x64.tar.xz\$" SHASUMS256.txt | sha256sum -c -     # 48cc0e21...aaf75  OK
mkdir ~/node-24.22.2 && tar -xJf node-v24.22.2-linux-x64.tar.xz -C ~/node-24.22.2 --strip-components=1
export PATH="$HOME/node-24.22.2/bin:$PATH"

# 2. Install exactly the lockfile. Install scripts are off (.npmrc ignore-scripts=true, and the flag says so again).
cd web
npm ci --ignore-scripts
```

Never `npm install` to "fix" a failing `npm ci`: change `package.json`, create the lockfile on Linux (below), commit both.

## Everyday commands (run in `web/`)

| Command | What it does |
|---|---|
| `npm run dev` | Vite dev server; `/api` is proxied to `http://127.0.0.1:8000` (start the API there, see below) |
| `npm run lint` | oxlint with the react and jsx-a11y plugins; the HTML-sink rules are errors (`.oxlintrc.json`) |
| `npm run typecheck` | `tsc -b` (TypeScript 7, `paths` only, no `baseUrl`) |
| `npm test` / `npm run test:coverage` | vitest + Testing Library (jsdom); coverage thresholds are 80% (`vitest.config.ts`) |
| `npm run build` | `tsc -b && vite build` into `web/dist` |
| `npm run e2e` | Playwright + axe against the fixture servers (below), over `web/dist` |
| `npm run lighthouse` | Lighthouse on the five pages, mobile and desktop; gates: accessibility 95, best-practices 90 |
| `npm run lighthouse -- --legacy` | the baseline of the old page at `/legacy` (recorded, not gated) |
| `npm run audit` | `npm audit --audit-level=high` |

The dev server needs the API: from the repository root, `uv run uvicorn semigraph.serve.main:app --host 127.0.0.1 --port 8000`
(it needs the Neo4j and keys of the real app; for UI work without them use the fixture server below).

## The fixture server and the end-to-end tests

`tests/e2e/serve_fixture_app.py` serves a built `web/dist` through the REAL routers and the real `serve/ui_v2.py` over fakes: no
Neo4j, no LLM, no `.env`, loopback only. It refuses to start with `ENVIRONMENT=production` or a provider key in its environment
(unset `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` for the shell that runs the tests).

```bash
# from the repository root, once: the Python side (the serve and agent extras)
uv sync --extra serve --extra agent

# in web/: build, then run everything; Playwright starts the two fixture servers itself (8765 preview, 8766 flipped)
npm run build
npx playwright install chromium          # a one-time browser download (add --with-deps on a fresh Linux box)
npm run e2e
npm run lighthouse
```

`REPO_ROOT` and `WEB_DIST` point Playwright and the Lighthouse runner at a repository and a `dist` other than the ones next to
`web/` (useful when `web/` is copied off OneDrive to keep `node_modules` out of a synced folder).

Python-side checks of this tree, from the repository root:

```bash
uv run pytest tests/test_web_pins.py tests/test_web_source_scan.py tests/test_dockerfile_web.py tests/test_dist_contract.py
```

`tests/test_dist_contract.py` skips its real-dist leg when there is no `web/dist`; run `npm run build` first. CI fails on that skip.

## Creating the lockfile (Linux only) and adding a component

The lockfile is created on Linux, never on Windows (a Windows-resolved lockfile can omit the Linux native binaries; a test pins
the `linux-x64-gnu` entries of rolldown, lightningcss and `@tailwindcss/oxide`). With Node 24.22.2 on PATH, in a Linux checkout
of `web/`, in CI or on a Linux machine. The commands, on Linux:

```bash
npm install --ignore-scripts            # only to change dependencies; commit package.json and package-lock.json together
npx shadcn@4.23.0 add <component> -y    # components come from the shadcn registry (base-vega style); see below
```

The shadcn CLI is NOT a dependency: it pulled `deepmerge` and `minimatch` (two osv-scanner findings) and an MCP server, babel and
dotenvx into every `npm ci`, and nothing in `web/` imports it (`components.json` is its configuration only). `npx` downloads
and runs it on the day a component is added (decision D17 allows that, on the owner's say-so), and `tests/test_web_pins.py`
fails if `shadcn` appears in `package.json` or the lockfile. Afterwards run `git diff package.json package-lock.json`: the CLI
adds the component's own runtime packages, and must not have added itself.

Rules for a copied-in component (`src/components/ui/`): no `dangerouslySetInnerHTML` or any other HTML sink (S1; `chart` is
never installed), no `transition-*`, `animate-*`, `duration-*` class and no `translate`/press nudge (nothing moves, R30), no
`"use client"` line. The installed set is button, badge, card, input, label, table, textarea. shadcn's init also writes a
`tw-animate-css` import, a web font and `shadcn/tailwind.css`; none of the three is used here (see the comment at the top of
`src/index.css`).

## Layout

```
web/
  index.html  vite.config.ts  vitest.config.ts  playwright.config.ts  .oxlintrc.json  tsconfig*.json  components.json
  src/main.tsx  App.tsx  routes.tsx  index.css
  src/lib/basename.ts  utils.ts  ids.generated.ts  pages.generated.ts   (the two generated files: uv run python -m scripts.export_ids)
  src/components/Layout.tsx  Nav.tsx  ui/*  (the shell: skip link, banner, named nav, main; ui/ is copied in)
  src/pages/*       one file per page of the generated page list, plus NotFound
  e2e/*.spec.ts     Playwright + axe;  scripts/lighthouse.mjs   the Lighthouse runner
```
