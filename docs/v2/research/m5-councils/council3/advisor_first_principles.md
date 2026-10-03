"Which framework" is the wrong question. The problem is: add five buyer-facing views without losing the contracts that make today's page trustworthy (strict CSP, textContent-only, diffed chip grammar, fail-closed Turnstile, sessionStorage-only). Judge every option by risk to those contracts.

What does the AI SDK actually buy? A streaming client. We already have one, and we forbid its main output path (our own restricted renderer, no markdown engine). Meanwhile the brief says it forces a NEW server encoder pinned by byte fixtures, with majors turning over about twice a year and the plan already one major behind (ai@6 vs ai@7). That is a new backend protocol surface plus a version treadmill, bought for a client we don't need. A Next.js static export that uses none of Next's features (no middleware, Server Actions, cookies) is a Vite SPA with extra footguns (generateStaticParams, trailingSlash under StaticFiles). F2 gives components, the a11y fixes and the same pages, and leaves the SSE grammar and SEC-path untouched. F3 fails on its own terms: five more views in a 1,040-line inline file. The owner's recorded choice (2026-09-25) predates this research; deviating is the owner's call, on this evidence.

Hosting: what does Cloudflare solve for M5? Nothing; one machine, caps already in place. H2/H3 break same-origin (CORS 403s, third-party cookies across PSL hostnames, CSP connect-src 'self' loosened), add a vendor, and H3 ends open access. Decide Cloudflare in M6.

Rollout: noindex `/v2/`; promote only when every old test assertion maps to a new test in a written table, CSP is identical, Lighthouse a11y >= 95, the keyboard gaps are closed, and `/legacy` stays.

Exposure: MCP and API keys gated by admin-issued keys with per-key quotas, off in production until the owner enables. Keep the dropped-risk badge omitted (audit: unreliable). Defer the follow-up question (extra LLM call, new template) as lowest value per risk.

Biggest risk: a security contract silently lost in the port; detect it with the mapping table plus CSP/XSS fixture tests in CI from day one.

Recommendation: H1, F2 (F1 only if the owner insists), MCP/API gated and off by default.
