# Decision 009: uploads that accept standard SEC/EDGAR filing sizes

Date: 2026-11-26. Asked by the owner: "in one of previous milestones we ended up reducing the limit of file upload some way, please
llm counsil the decision, I simply want to make it accept standard file sizes, you know the reports sizes of SEC and EDGAR, if not do
analysis of it best case, worst case and then decide on file size." Research (workflow wf_22aa302b-473, read-only) measured the 73
local EDGAR filings with the real upload parser and re-checked hosting and embedding options live; then the `llm-council` skill
(workflow wf_1b454e01-f15): five advisors (Sonnet), five anonymised peer reviews (Sonnet), a chairman (Opus).

Status: **council advice; the owner decides.** INTERNAL document.

## Facts given to the council

- What blocks real filings is the 10,000-token cap per document (lowered from 16,000 by council 9 on 2026-11-06), not the file
  size cap: the 15 MiB byte cap was never lowered and covers every sampled 10-K and 10-Q (3 of 8 20-Fs exceed it). The token cap
  follows from the measured worst-case embedding speed on the shared CPU (5.54 tokens/s; typical 12-36; a dedicated performance
  core 85.8) and the 2,100 s embed ceiling.
- Whole filings, as the upload parser sees them (Qwen tokens): 10-K median 92k, max 140k; 10-Q median 52k, max 93k; 20-F median
  231k, max 326k. Risk-factor sections: 10-K median 17.7k, max 39.5k. The parser does not yet strip inline-XBRL hidden facts.
- Options: B background job with progress, markup stripping, section-first embedding and immediate keyword search ($0/month);
  C a dedicated-CPU machine (+$40-51/month); D a hosted embedder serving the same model (pennies; a new vendor receives private
  uploads; vector drift); E a per-upload worker machine; F section-split uploads only.

## Chairman's verdict (verbatim)

## Where the Council Agrees

- **Accepting a file and embedding it are separate decisions.** All five advisors and all five reviews say this. The size limit can rise a lot because bytes only reach the sandboxed parser. The embedding limit cannot rise much on this CPU.
- **Keyword (BM25) search over the whole upload, available as soon as parsing ends, is the core fix.** It costs $0, needs no model, and M2 needs the same code, so build it once and use it for both.
- **Strip the hidden inline-XBRL facts before setting any token number.** Every whole-filing count in the question is inflated until that is done.
- **Embed by section, most important first.** Default to the sections that answer most questions. Everything else stays keyword-only, or is embedded only if time is left.
- **Do it as a background job with real progress shown**, not a request that blocks.
- **Reject C for now** (+$40-51/month, roughly doubling the bill). **Defer E** (complexity, and private data leaves the machine). Four of five advisors **reject D** (hosted embedder): it changes the privacy promise, its vectors drift from ours, and it adds a vendor the owner has not approved.
- **Raise file bytes to 35-40 MiB.** That covers every measured filing, including the largest 20-F (33.9 MB).

## Where the Council Clashes

1. **How many tokens to embed.** Executor: 20k guaranteed. First Principles: 45k. Outsider: about 40k. Expansionist: 150k with no time limit. Contrarian: do not change it before measuring. The production check refuses any cap above ceiling x 5.54 tok/s, and that settles it: 45k and 150k cannot run on this machine. The Executor's 20k at a 60-minute ceiling (~19.9k) is the only proposal that is consistent with itself. The Contrarian is right that the floor speed is the binding fact.
2. **Hosted "fast embed" as an opt-in (Expansionist) versus rejecting it outright (the rest).** Rejected for now. It is a new vendor and a change to the privacy promise, and the owner has approved neither. It also brings vector drift and a dependence on the vendor's uptime.
3. **Embed time limit: 35 minutes, 60 minutes, or none.** "None" is rejected: a single shared CPU needs a limit. 60 minutes is accepted, but only together with the daily-count fix below, which the Executor missed.
4. **Whether to promise "every filing, whole".** The answer: every 10-K and 10-Q in the sample is *accepted and searchable* whole, by keyword. Semantic (vector) search over the whole filing is *not promised*.

## Blind Spots the Council Caught

- **The daily time budget (no advisor caught this).** The 40-a-day rule exists because 40 x 2,160 s = 86,400 s, one full day of a single embedding worker. A 60-minute limit therefore allows at most **24 embedding jobs a day**. The fix is to keep accepting 40 uploads a day. Uploads beyond the 24th embedding job of the day become keyword-only, so an upload is never refused for lack of embedding time.
- **20-F section numbers differ from 10-K numbers.** The advisors applied "Items 1, 1A, 7" to 20-Fs, which is wrong. The priority sections are:
  - **10-K:** Item 1A (risk factors), Item 7 (management's discussion), Item 1 (business).
  - **10-Q:** Part II Item 1A (risk factors), Part I Item 2 (management's discussion).
  - **20-F:** Item 3.D (risk factors), Item 5 (operating and financial review), Item 4 (company information).
- **Section detection is not reliable.** Our own data shows the sec-parser workaround and the custom-layout fallbacks for Intel and ASML. Measure the detection hit rate on the 73 filings before relying on it. When no sections are found, embed in document order and tell the user.
- **Contention with live questions.** Use one embedding worker and a first-in-first-out queue. Run it at the lowest CPU priority (nice 19), in small batches that yield between batches, so live questions and M2's keyword and reranker work come first. The job must reuse the model already loaded in memory: a second 1.26 GB copy would crash the 4 GB machine.
- **Jobs must survive restarts.** Store job state with the workspace. After a restart or deploy, resume from the last embedded chunk; if that fails, mark the document "keyword only". Add a cancel button. The 24-hour workspace expiry is fine with a 60-minute job.
- **Parse memory and file bombs.** Before raising the byte limit, parse the 33.9 MB 20-F in the real sandbox and measure it. Also:
  - Cap DOCX uncompressed size, to stop zip bombs.
  - Refuse a file early with a cheap character-count check after stripping, before tokenizing, so a 40 MB TXT file is not tokenized for nothing.
- **The 30-page limit blocks every PDF 10-K** (~100-250 pages). Fixing bytes alone does not help PDF uploads.
- **What was actually lowered.** The owner remembers the upload limit being cut. File bytes were never lowered (still 15 MiB). The per-document token cap was cut from 16,000 to 10,000 on 2026-11-06. Tell the owner this plainly.
- **Fetch by accession number** (Review 4) is a good later option. A public filing pulled straight from EDGAR skips both the upload size problem and the private-data objection to a hosted embedder. It still needs vendor approval and a fix for drift, so it is a reopen option, not part of this change.
- **The sample is small**: 8 20-Fs and a few dozen 10-Ks and 10-Qs. Treat the sample maximum as roughly the 95th percentile and add 15% headroom.

## The Recommendation

**Architecture:** B with F as the default behaviour. Same machine, **$0/month, no new vendor, no new machine.** C, D and E are deferred with a stated reopen trigger (below).

**Limits.** Values marked * are provisional and get reset from the post-strip measurement: sample maximum x 1.15.

| Setting | Now | New |
|---|---|---|
| File bytes (all types) | 15 MiB | **40 MiB**. Gated on the 33.9 MB 20-F parsing inside the existing 1 GiB / 120 s CPU sandbox. If it fails, HTML stays at 15 MiB until the parser streams. |
| PDF pages | 30 | **300**. Gated on a sandbox parse of a real 10-K PDF. No OCR. |
| DOCX uncompressed size | none | **150 MiB** |
| Parsed tokens per document (accepted, keyword-searchable) | 10,000 | **160,000\***. Covers every sampled 10-K (max 140k) and 10-Q (max 93k) whole. 20-F: the priority sections plus the first 160k\* tokens are searchable. Anything past that is cut off, and the user is told which sections were dropped. |
| Keyword chunks per version | 120 | **~320** (160k / 512) |
| Guaranteed embedded tokens per document | 10,000 | **19,900** (60 min x 5.54 tok/s). Change the production check to validate this *guaranteed* figure. |
| Best-effort embedding | none | Priority sections continue past 19.9k while the time limit allows; at typical speeds of 12-36 tok/s that is about 43k-130k. Whatever is not embedded stays keyword-only. |
| Embedded tokens per workspace | 48,000 | **48,000** (unchanged) |
| Documents per workspace | 3 | **3** (unchanged) |
| Embedding time limit | 2,100 s | **3,600 s** |
| Uploads per day (global) | 40 | **40**, of which at most **24 get embedding jobs**; the rest are keyword-only. |
| Per IP | 10/hour | **10/hour**, plus **one active embedding job per IP and workspace** |
| Memory guard / parse sandbox | 1.25 GB free; 1 GiB / 120 s | **Unchanged** |

**What the user sees:**
1. On upload, a preflight card: the detected form (10-K, 10-Q or 20-F), the parsed tokens, the sections found, and an estimated wait.
2. Once parsing ends (seconds to about 2 minutes): **"Keyword search ready."** Questions work immediately.
3. A progress bar, for example "Semantic search: Item 1A 62%, about N min left", with the time estimate based on the speed measured during the job. If another job is ahead, the queue position is shown.
4. Each answer is labelled **keyword-only** or **keyword + semantic**.
5. At the end: "Embedded: Items 1A, 7. Keyword-only: the rest."

**Best and worst case for each option:**

| Option | Best case | Worst case |
|---|---|---|
| **B + F (chosen)** | A 10-K is keyword-searchable in seconds, and its risk factors are embedded in 3-8 minutes. | At 5.54 tok/s only 19.9k priority tokens are embedded after 60 minutes, and the rest stays keyword-only. Nothing crashes, and no money is spent. |
| C (dedicated CPU) | About 85.8 tok/s: a whole 10-K embedded in about 18 minutes. | +$40-51/month every month for a demo with few users. A whole 20-F still takes about 63 minutes. |
| D (hosted embedder) | Seconds and fractions of a cent. | A privacy breach of trust, silent drift, and a vendor outage that breaks upload search. Needs owner approval. |
| E (worker machine per job) | Pennies per job, fast. | Weeks of work, private data crosses machines, and cold starts. |
| F only | Cannot fail. | The product shrinks to risk sections only. |

**Monthly cost:** $0.

**Reopen trigger, which goes to the owner and is not acted on automatically:** after 20 real uploads, if more than 30% of priority sections are not finished within 60 minutes, or the measured 95th-percentile speed is below 10 tok/s, bring C or E back for approval. Decide machine size jointly with M2's reranker, not separately.

## The One Thing to Do First

Run one offline measurement pass, at $0, before changing any limit:
1. Add inline-XBRL stripping to the parser.
2. Re-parse all 73 local filings. Record the post-strip token counts and the section-detection hit rate per form (10-K Items 1, 1A, 7; 10-Q Part I Item 2 and Part II Item 1A; 20-F Items 3.D, 4, 5).
3. Parse the 33.9 MB 20-F in the real sandbox and record peak RSS and CPU seconds.

These numbers fix the provisional caps (\*). They decide whether 40 MiB is safe, and whether section-first ingestion can be relied on.

## Owner decision

Pending.
