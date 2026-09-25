**REPORT: cheaper inference (2026-09-25). No repo file changed.**

Every price, limit and status below was read live on 2026-09-25 unless marked UNVERIFIED. Scratch scripts and their outputs are in the session scratchpad.

**Disclosure.** While cleaning up I ran `taskkill` on python.exe PID 34892 by mistake. It started 18:19, before my work, and was not my benchmark process. It is probably one of your own processes (Jupyter kernel or language server), so check whether it needs restarting.

**Verified prices, $/Mtok** (sources: [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing), [Gemini](https://ai.google.dev/gemini-api/docs/pricing), [OpenAI](https://developers.openai.com/api/docs/pricing), [DeepSeek](https://api-docs.deepseek.com/quick_start/pricing), OpenRouter `/api/v1/models` and `/endpoints`)

Batch is 50% off on Claude, OpenAI, Gemini and Alibaba. DeepSeek has no batch API but is 50% cheaper off-peak. Costs assume 16k in / 1.5k out per answer, and 1.3k in / 0.3k out per extracted chunk.

| Model | In/out | 1 answer | 1,000 answers | Extract 500 / 1,500 chunks (batch) |
|---|---|---|---|---|
| Sonnet 5 (today) | 2/10 | $0.047 | $47 | $2.8 / $8.4 ($1.4 / $4.2) |
| Haiku 4.5 | 1/5 | $0.0235 | $23.5 | $1.4 / $4.2 |
| GPT-6 Sol | 2/10 | $0.047 | $47 | no saving |
| **GPT-6 Luna** (1.05M ctx) | 0.10/0.50 | $0.0024 | $2.35 | $0.14 / $0.42 ($0.07 / $0.21) |
| Gemini 3.8 Flash | 0.75/3.75, doubling 2027-01-01 | $0.0176 → $0.0352 | $17.6 → $35.3 | $1.05 / $3.15 |
| Gemini 3.5 Flash-Lite | 0.30/2.50 | $0.0086 | $8.55 | $0.57 / $1.71 |
| DeepSeek V4.1 Flash | 0.30/1.20 peak, 0.15/0.60 off-peak | $0.0066 / $0.0033 | $6.6 / $3.3 | $0.38 / $1.12 |
| Qwen3.8-Flash (Alibaba) | 0.15/0.47 | $0.0031 | $3.1 | $0.17 / $0.50 |
| gpt-oss-120b | 0.03/0.17 cheapest host, 0.15/0.60 Groq/Together | $0.0007 – $0.0033 | $0.73 – $3.3 | n/a |

- A reranked 6.5k prompt cuts each figure by 40–60%. Sonnet 5 falls to $0.028 and Luna to $0.0014.
- Claude 4.7+ tokenizes about 30% more tokens. Non-Claude costs are therefore overstated by roughly 25%.
- Haiku 4.5 is "Active, retirement not sooner than 2026-10-15" ([deprecations](https://platform.claude.com/docs/en/about-claude/model-deprecations)). Anthropic promises 60 days' notice. There is no newer Haiku.
- Sonnet 5's $2/$10 became the permanent price.
- Luna's model page ([OpenAI](https://developers.openai.com/api/docs/models/gpt-6-luna)) says Chat Completions supports function calling only at `reasoning_effort=none`. The agent needs `use_responses_api` if it wants reasoning.
- LiteLLM's main-branch price map has `gpt-6-luna`, `gpt-6-sol`, `gemini/gemini-3.8-flash`, `dashscope/qwen3.8-flash` and `deepseek-flash`. I did not confirm they ship inside the 1.102.1 wheel.

**Quality evidence, all weak for our use**
- Artificial Analysis Intelligence Index ([AA leaderboard](https://artificialanalysis.ai/leaderboards/models), fetch summaries):

| Model | Index |
|---|---|
| Gemini 3.8 Flash | 41 |
| DeepSeek V4.1 Flash | 39 |
| Sonnet 5 | 38 |
| Luna | 37 |
| Qwen3.8-27B | 34 |
| Gemini 3.5 Flash-Lite | 22 |
| Haiku 4.5 | 17 |
| gpt-oss-120b | 12 |

- AA-LCR (long context) is a tie: DeepSeek V4.1 Flash 84, Luna 83, Sonnet 5 82, Gemini 3.8 Flash 81.
- All of these were measured at max or high reasoning effort. Time to first token was 109 s (Luna) and 150 s (Sonnet 5). My costs assume no reasoning, so quality at effort none or low is unmeasured. The bake-off must pin effort per model.
- The AA-Omniscience values in the fetched pages contradict each other, so I discarded them. They measure closed-book knowledge anyway.
- Vectara HHEM ([README](https://github.com/vectara/hallucination-leaderboard), updated 2026-09-22, short-document summarization):
  - Sonnet 5, Luna, Gemini 3.5–3.8 Flash and DeepSeek V4.1 Flash have no row.
  - Nearest rows: gpt-5.4-nano 3.1%, gpt-5.4-mini 5.5%, gpt-6-sol 6.5%, Haiku 4.5 9.8%, Sonnet 4.6 10.6%, gpt-oss-120b 14.2%.
- FACTS Grounding: Kaggle returned only a JavaScript shell. "Gemini 3.8 Flash 73.1%" came from a search snippet, so UNVERIFIED.
- No independent RAG-citation or tool-calling number exists for any candidate. Our own gold set is the only real evidence.

**Capacity for 10,000 concurrent users**

Assumption: one question per user every 120 s. That is 5,000 RPM, 80M input tokens/min at 16k context, or 32.5M at 6.5k.
- **Claude Scale tier:** Sonnet 5 gets 10k RPM, 10M ITPM and 2M OTPM ([rate limits](https://platform.claude.com/docs/en/api/rate-limits)). That fails on input and output, so you would need a Custom tier.
- **GPT-6 Luna:** Tier 5 (30k RPM, 180M TPM) fits. Tier 4 (10M TPM) does not.
- **Gemini:** the Tier 3 spend limit is $200 per 10 minutes. The load costs about $525–880 per 10 minutes, so it is blocked without a custom arrangement.
- **DeepSeek:** the flash model allows 2,500 concurrent requests. I estimate about 620 in flight, which fits. The first-party API is China-hosted. US hosts are DeepInfra ($0.14/$0.42) and Fireworks ($0.22/$0.66).

**Free tiers are dev-only**
- Groq free gives gpt-oss 8K TPM, which cannot hold one 16k prompt.
- OpenRouter `:free` allows 20 RPM and 50 requests/day (1,000 with $10 bought).
- Cerebras free is 5 RPM and 1M tokens/day.
- GitHub Models was retired 2026-07-30.
- NVIDIA NIM is prototyping-only (UNVERIFIED, secondary sources).
- Gemini's free tier uses your prompts for training ([terms](https://ai.google.dev/gemini-api/terms)). Its daily quota for 3.8 Flash is disputed: a Google forum post says 20 requests/day, blogs say 1,500. UNVERIFIED.
- Mistral's free plan gives $10/month in API credits.

**Non-swap levers**
- **Prompt caching saves about $0 on the answer path.**
  - `answer.txt` is 835 bytes, and the question comes before the context.
  - That prefix is well under Sonnet 5's 1,024-token cache minimum (Haiku 4.5: 4,096). `extractor.txt` is also under it.
  - Caching only helps the agent loop, where history and tool schemas grow each turn.
- **Reranking:**
  - `k_chunks=8` at 700–1,100 tokens each is about 5.6–8.8k of the 12–21k prompt.
  - The rest is graph blocks (2-hop edges, up to 20 metrics, risks, temporal). A cut to 5–8k also needs those blocks capped.
  - Instrument the per-block token breakdown first.
- **Batch:** extraction costs at most $8.40 even on Sonnet 5, so it is not worth optimizing. Swapping the extractor risks schema drift against the existing 8,082 risk factors.
- **Extractor critic:** `extractor.py:204` makes a per-chunk critic call on Haiku 4.5 (`config.py:22`), which is not in my numbers. It must leave Haiku 4.5.
- **Semantic cache:** LiteLLM warns semantic caches embed the whole message array and misbehave on agent traffic ([docs](https://docs.litellm.ai/docs/proxy/caching_semantic)). Use a query-level cache instead, keyed on question, entity set and data-snapshot id. Nvidia vs AMD near-misses and per-refresh invalidation are real risks. Do not assume a hit rate.
- **Router:** LiteLLM `fallbacks`, `context_window_fallbacks` and `num_retries` are confirmed in the docs.
- **Self-hosting:** not recommended.
  - Prefill-heavy load at 83 rps needs dozens of GPUs.
  - gpt-oss-120b is weak (AA index 12, HHEM 14.2%).
  - H100 rentals run $1.49–6.98/hr and vLLM throughput is 1–6k tokens/s (both UNVERIFIED, from search snippets).
  - Self-host only the embedder, reranker and HHEM critic.

**Rerankers**

Timings are upper bounds: 24-core laptop, 2 threads, fp32, 50 pairs of about 350 tokens.

| Model | License | Size | Measured |
|---|---|---|---|
| MiniLM-L6 | Apache-2.0 | 23M | 2.2 s (44 ms/pair) |
| bge-reranker-v2-m3 | Apache-2.0 | 568M | 54.6 s (1.09 s/pair) |
| Qwen3-Reranker-0.6B | Apache-2.0 | 0.6B | 86 s |
| mxbai-rerank-base-v2 | Apache-2.0 | 0.5B | not measured |
| jina-reranker-v3 | CC-BY-NC-4.0 | 0.6B | non-commercial, exclude |
| FlashRank | Apache-2.0 | small | last release 2025-01-06, stale |

- Qwen3-Reranker is a yes/no language-model scorer. Loaded as a CrossEncoder, its latency is valid but its scores are not.
- CPU reranking of 50 candidates per query at about 33 rps needs roughly 145 cores. Use a GPU or hosted reranker, or rerank the top 20 only.
- Hosted options: Cloudflare bge-reranker-base is $0.003/Mtok, about $0.00005 per query. Cohere is about $2–2.5 per 1,000 searches (UNVERIFIED).
- HHEM-2.1-Open (Apache-2.0, 110M parameters) is a free CPU candidate for an online faithfulness gate.

**Recommended lineup**

| Role | Model | Cost / note |
|---|---|---|
| Answerer | GPT-6 Luna, effort none or low | $0.0024 (16k) or $0.0014 (6.5k), 20–33× cheaper |
| Escalation | Sonnet 5 when the citation verifier fails or the agent has low confidence | 10% escalation gives about $0.0068/answer, about 7× cheaper |
| Agent / tools | Gemini 3.8 Flash or Luna via the Responses API, chosen by a tool-call eval | 3.8 Flash saves only 1.3× after 2027-01-01, not durable |
| Extractor | Keep Sonnet 5 Batch ($1/$5) | about $4 per 1,500 chunks |
| Critic | Gemini 3.5 Flash-Lite or DeepSeek V4.1 Flash, plus HHEM gate | replaces Haiku 4.5 |
| Judge | Sonnet 5 plus Gemini 3.8 Flash as second judge | cents per run; different family from the answerer, comparable with v1 |
| Fallback chain (LiteLLM) | Luna → DeepSeek V4.1 Flash (US host) → Gemini 3.8 Flash → Sonnet 5 | |

Luna caveats: it launched on 2026-09-22 (three days ago), it is verbose, and it hallucinated 77% on AA's closed-book test. Rely on a strictly grounded prompt plus citation validation.

**Bake-off (re-run the 20-question gold benchmark; do not swap without it)**
- Sonnet 5 as baseline.
- Luna at effort none and low.
- DeepSeek V4.1 Flash, non-thinking, on a US host.
- Gemini 3.8 Flash at low effort.
- Gemini 3.5 Flash-Lite.
- Qwen3.8-Flash.
- Optional: gpt-5.4-nano and gpt-5.4-mini (HHEM proxy leaders), and Haiku 4.5 as a control.

Record per model: faithfulness, `CITE_RE` citation validity, correctness, refusal on unanswerable questions, TTFT, tool-call accuracy and cost.
