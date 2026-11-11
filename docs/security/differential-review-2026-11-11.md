# Differential security review: M5b redesign (2026-11-11)

**Range:** `origin/v2..f1c5a38`, 48 commits.
**Scope:** `src/`, `web/src/` (non-test), `web/index.html`, `web/vite.config.ts`. That is 81 code files: 47 modified, 32 added, 2 deleted. Artifacts, docs and tests were read only as evidence.
**Method:** workflow `wf_0e6c0f22-bf1`. Three Opus reviewers each covered one lens, and an Opus refuter was set to challenge each candidate finding.

**Verdict: no findings.** No lens reported a candidate, so the refuter step had nothing to check. Every changed security-relevant site was read and judged safe. The other pre-push checks on the same range were also clean:

- `/security-review`: no findings.
- gitleaks: 48 commits, no leaks.
- semgrep (`p/python`, `p/security-audit`, `p/typescript`, `p/react`) against the baseline `origin/v2`: no new findings.

## Risk triage

| Area | Files | Risk | Why |
|---|---|---|---|
| Server | `serve/ui_v2.py`, `retrieval/dossier.py` | MEDIUM, read in full | Server-side changes |
| Links built from data | `EvidenceBody.tsx`, `SpecimenStamp.tsx`, `lib/specimens.ts`, `lib/evidenceView.ts`, `AskParts.tsx` | HIGH, read in full | External URLs rendered as anchors |
| Evidence fetch | `evidenceFetch.ts`, `lib/specimenCache.ts` | HIGH, read in full | Builds a URL from a model-cited id |
| Hash and navigation state | `lib/sectionHash.ts`, `useSectionFocus.ts`, `Layout.tsx`, `Ask.tsx` (askAbout) | HIGH, read in full | Input the attacker controls through the URL or history |
| Workspace token | `Workspace*.tsx`, `lib/workspace.ts`, `state/*` | HIGH, read in full | A bearer secret |
| Pattern matching and highlighting | `lib/highlight.ts`, `answerSources.ts`, `starters.ts`, `hedge.ts` | MEDIUM | Patterns and text segments derived from data |
| Copy deck, CSS, fonts, chips, stamps | `copy/deck.ts`, `index.css`, the rest | LOW | Text and presentation |

## What was verified

1. **Removed code.**
   - Two files were deleted, `pages/Ask.copy.ts` and `pages/Workspace.copy.ts`. Both held copy only, which now lives in `copy/deck.ts` and `Ask.constants.ts`.
   - Every removed `safeUrl` call, `classifyId` check, `encodeURIComponent`, `rel` attribute and id or token grammar check has an equivalent at HEAD:
     - the drawer anchor moved to `EvidenceBody.tsx`;
     - `Cite` moved to `CompanySection.tsx`;
     - `FigureStamp` is gated on `classifyId === 'xbrl'`.
2. **Server.**
   - `ui_v2.py` adds only `mimetypes.add_type('font/woff2', '.woff2')`. Routes, path checks, `nosniff` and the CSP are unchanged.
   - `dossier.py` raises `DOSSIER_METRIC_PERIODS` from 2 to 4. It is a constant passed to Cypher as a parameter and returns only public XBRL periods.
3. **Sinks.**
   - Non-test `web/src` contains no `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `eval`, `new Function`, `window.open` or `location.*` assignment.
   - Every href taken from data goes through `safeUrl` (lowercase `https://` only, no control characters, no credentials, a host required) and carries `rel="noopener noreferrer"`.
   - Link labels name the filing or the rule only for sec.gov or federalregister.gov hosts.
   - Every dynamic id and aria reference is a `useId()` value or a constant.
   - The one `querySelector` built from data uses a chip key whose id has already passed the anchored grammar, and it escapes `"` and `\`.
   - Patterns are built only from constants or from escaped, generated FILERS names. The highlighter uses `indexOf`, and every segment renders as a text node.
4. **Hash and navigation state.**
   - The hash is compared exactly against fixed id lists, and only a match reaches `getElementById`.
   - `askAbout` is accepted only when it passes `isFilerTicker`, is then cleared, and is never sent anywhere.
5. **Workspace token.**
   - The token is read from an uncontrolled `type="password"` field with `autocomplete="off"` and cleared after use. Closing the disclosure unmounts the field.
   - It never reaches React state, a `value` attribute, a URL, logs, aria attributes or localStorage. sessionStorage is unchanged from before.
   - `WORKSPACE_ID_RE` and `TOKEN_RE` are unchanged.
   - Uploader names now render inside `<bdi>`, which is stronger bidi isolation than before.
6. **Build.**
   - `index.html` adds only two same-origin font preloads.
   - `vite.config.ts` still never inlines fonts as `data:` URIs.
   - No change to the CSP or headers.

## Coverage limits

- The deck, CSS and some of the Company pages were checked by grep rather than a line-by-line read. These are text and presentation files with no URL, `location` or sink strings.
- The cause of the run-to-run variance in highlight hits (decision 003) is out of scope; it is not a security issue.
- Tests were not re-run here. The stage-3 evidence in `docs/specs/m5b-redesign.md` covers them.
