F1 buys a rewrite whose benefit the brief never shows and whose costs it lists. It forces a new FastAPI encoder (no first-party Python one exists), pins ai@6/@ai-sdk/react@3 while ai@7 is current, then bypasses the SDK's rendering with our own plain-text-plus-chips renderer. What's left is a state hook with twice-yearly major churn. Meanwhile today's page holds a strict CSP, fail-closed Turnstile with a fresh token per ask, and 149 tests pinning the old file; none port automatically. Assumptions (not in the brief): Next static export emits inline hydration scripts and, with no middleware, cannot nonce them, so the strict CSP either loosens or needs per-build hashes; basePath is build-time, so the artifact proven at /v2/ is not the one promoted to /. The three known gaps are in-place fixes.

H2 is worst: a Worker in front of one IP-capped machine likely collapses all visitors into one IP (assumption: caps key on client IP), and adds 429s above 100k/day plus SSE buffering risk. H3 ends open access and needs payment details; defer to M6.

M5c: MCP and API keys are a scraping surface on the shared Neo4j; ship dark, keyed-only. The follow-up rewrite is an injection path, and it must count against the 150/day cap (brief is silent). Admin console auth is unspecified. Recommend deferring the supply-chain map, API keys/MCP and admin console.

Biggest risk: silently losing a security contract. Detect: port all 149 tests plus a CSP-header diff as a gate before building any new view.

Recommend H1, F3 (split into native ES modules, no build; F2 fallback; F1 only if the owner overrides), and MCP/API keys dark and keyed-only, never anonymous-open.
