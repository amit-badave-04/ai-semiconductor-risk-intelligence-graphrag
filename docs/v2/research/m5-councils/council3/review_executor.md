**1. Strongest: D.** It judges every option by the security contracts at risk. It requires a written table mapping each old assertion to a new test, which is the only direct answer to the 149-test port. It also gates on an identical CSP and defers the lowest-value follow-up. B is the runner-up for its concrete Docker trailing-slash smoke test.

**2. Biggest error: C.** It says Next's static export forces the "strict CSP" to loosen. The addendum corrects this: today's CSP already allows `'unsafe-inline'`, and Next's no-nonce policy matches it. B and D also lean on "strict CSP". With CSP neutral across F1/F2/F3, the F1-vs-F2 split rests on encoder cost and SDK churn, which the addendum says are unmeasured. A's F1 case ("any AI SDK client could consume it", AI Elements polish) has no evidence behind it.

**3. All missed:**
- Port cost. The tests pin one inline file, and F1 and F2 both force an npm toolchain and a re-port; only F3 keeps them cheap. Nobody estimated it.
- Buyer experience. An unlinked `noindex` `/v2/` means no buyer sees it or gives feedback. Deep links and bookmarks at cutover are unaddressed.
- A11y. Lighthouse >= 95 will not catch keyboard traps. A manual keyboard test is needed.
- Turnstile. A fresh token per ask must survive React remounts.
- MCP and keys. `search_chunks` shares the single machine and embedder with the live ask path. Per-key quotas do not bound global cost. No consumer is named (only E notes this).
- Build-time base path. The promoted artifact is not the one that passed the gates (only C notes this).
