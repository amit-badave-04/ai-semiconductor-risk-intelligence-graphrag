# 005 Agent spend budget for M2a's new context (llm-council, 2026-11-22)

**Question.** The owner said: "I raise its estimate ceiling, llm counsil it" (2026-11-22). R4 does not fit the agent's ceilings; raising the agent ceiling alone empties the per-address share window. Council: 5 advisors and 5 anonymous reviewers on Sonnet, chairman on Opus (workflow wf_e298d86d-a56). Owner's decision on the verdict (2026-11-22, the option the owner selected): "Council plan (Recommended)": the agent stays at $1.02 with partial R4 if it measures in (else R4 off for the agent); hybrid trimmed to N=6 quotes and 120-character abstracts at a 46,000-character per-anchor allowance; no spend cap or production guard changes; the $1.07 raise is revisited after the paid eval from real costs (option d).

## Chairman's verdict

## Where the Council Agrees

- **Reject (b).** Four of five advisors, and every peer review, refuse to raise the $10 day cap. It is the owner's own money (about $25 left), and the change would move hard-coded production guards, the fly env, the RUNBOOK and pinned tests. All of that would buy one citation id the agent already prints on its own planner-computed lines.
- **Hybrid: trim it, don't raise it.** Four of five advisors pick N=6 quotes, a 120-char abstract cut and a 46,000-char allowance, for an estimate of about $0.743. At 48,000 chars the margin is $0.0009, and the eight-address bound becomes (10 - 0.759145)/7 = $1.32012. That leaves only $0.00012 of headroom over S = $1.32. Any rounding or render change breaks it.
- **The window checks out with the trim.** The upper bound is (10 - 0.743)/7 = $1.3224. The lower bound is 0.30 + 1.016281 = $1.3163. S = $1.32 sits inside both, so no guard needs to move.
- **Run the paid eval once, on the configuration that will ship.** Tune the caps afterwards, as a separate reviewed change, and only from measured costs.
- **(d) cannot inform this decision.** Its number does not exist until the eval has run.

## Where the Council Clashes

- **Whether to honor the owner's literal ask.** The Expansionist takes (c): agent ceiling $1.07, S = $1.38, seven addresses to pause the site. Its case is that the agent is the feature buyers notice, and R4 makes it look institutional-grade. Everyone else says the owner's real goal is "keep the agent," not "keep R4." Since the agent's computed lines already carry both years' ids, R4 does little for the agent. The majority is right on the merits. Every reviewer still flagged that the majority is overruling an explicit owner instruction, so the owner has to make that call, not the council.
- **(e) partial R4 versus (a) R4 off.** The Executor and the First Principles Thinker want (e), with (a) as the fallback. The Outsider wants plain (a), because (e) is one more rule to explain and test. The Contrarian allows (e) only if a test proves it fits. The split resolves on measurement: nobody has rendered (e) yet.
- **How to handle an (e) overflow.** The Executor would drop the oldest noted rows to make it fit. Reject that, because it silently removes data. If (e) overflows, fall back to (a).

## Blind Spots the Council Caught

1. **The owner said "raise its estimate ceiling."** As stated, that cannot be done without (b) or (c). The council must say so plainly and give the owner the choice before the paid eval, not report a downgrade as if it were the ask. The Executor's line "raised as far as the caps safely allow, which is not at all" spins a refusal as compliance. Do not use it.
2. **(e) is unmeasured.** The estimate is priced from the allowance, not from the actual render. So (e) passes only if the worst-case render fits inside the existing allowances: at most 50,000 chars per company and 470,324 in total. If it does, the estimate stays at $1.016281. This check is a free offline render, with no LLM spend.
3. **Citation integrity.** Nobody checked whether agent year-over-year answers without a base-year id still pass the M2a answer-integrity and grounding check. That is a free offline test and has to pass before the eval.
4. **The hybrid trim costs quality.** Going from N=10 to 6 quotes and from 200 to 120 abstract chars cuts into R2, R5 and R6, the features M2a was built for. The eval must include quote and abstract cases so that cost is measured, not assumed.
5. **No test pins the window.** The agent margin is $0.0037 and the hybrid margin is $0.017. Nothing fails CI if a later change pushes an estimate past its target or empties the S window.
6. **(d) is the cheap, honest route to the owner's ask.** The full-R4 agent at $1.0693 fits S = $1.32 unchanged if the "$0.30 already settled" figure turns out to be at most 1.32 - 1.0693 = **$0.2507**. Then no production guard moves at all. The other advisors brushed (d) aside; it is the one path that delivers what the owner asked for at no new exposure.

## The Recommendation

**Options: hybrid trim, agent (e) if it measures in or else (a), and (d) after the eval as the path to the raised ceiling. Not (b). Not (c) unless the owner explicitly accepts its cost.**

- **Hybrid:** N = 6, abstract cut = 120 chars, per-anchor allowance = 46,000 chars, estimate about $0.743, target at most $0.76. R4 stays fully on for hybrid.
- **Agent:** the ceiling **stays at $1.02**, with the estimate at $1.016281 and the per-company allowance at 50,000. R6b keeps quotes and abstracts off.
  - R4 runs partially (e): the base-year id goes only on the rows the question names plus the latest row.
  - (e) ships only if the worst-case render measures at most 50,000 chars per company and at most 470,324 in total.
  - If it does not fit, R4 is off for the agent (a). Do not drop rows to force a fit.
- **Per-address share S:** $1.32, unchanged. The legal window is $1.3163 to $1.3224.
- **Day cap:** $10.00, unchanged.
- **Production guards:** none change (S ≤ 1.32, day cap ≤ $10, the eight-address rule). No pinned guard tests change.
- **New test:** add a CI test that fails if any of the four estimates exceeds its target, or if the S window (both bounds) no longer contains $1.32.
- **Eval:** one run on this configuration. It must include:
  - agent year-over-year cases, as evidence on R4;
  - hybrid quote and abstract cases, to measure the cost of the trim;
  - logging of real settled costs per ask type, to feed (d).
- **After the eval, path (d):** re-derive the $0.30 figure from the real settled costs.
  - If it comes out at most $0.2507: bring a reviewed change that raises the agent ceiling to $1.07 (allowance 56,500, estimate $1.0693) with full R4, keeping S at $1.32 and the cap at $10. Then re-run only the agent slice of the eval.
  - If it comes out higher: the agent keeps $1.02.
- **If the owner insists on the raise now:** the only acceptable form is (c). Agent ceiling $1.07 (estimate $1.0693), S = $1.38, day cap $10. The window under (c) is $1.3693 to (10 - 0.743)/6 = $1.5428. The cost: the S guard moves from 1.32 to 1.38 (a reviewed change with re-pinned tests), each address's daily share rises by $0.06, and **seven addresses instead of eight** can pause live asks for the day. Never (b).

**What to tell the owner:**
"You asked me to raise the agent's estimate ceiling. On its own that cannot be done. It only fits if I also raise your $10 daily cap, or let seven addresses instead of eight pause the site for the day. The council recommends neither yet. The agent stays at $1.02 and keeps everything except a prior-year id on single computed notes, which its own computed lines already carry. Hybrid trims to 6 quotes and 120-char abstracts so it fits with a safe margin. Nothing about your spend limits changes. After the paid eval, if real costs show the demo uses at most about $0.25 before the second agent ask, I can raise the agent ceiling to $1.07 with no cap change at all. If you want the raise now anyway, it is option (c): S goes to $1.38 and seven addresses can pause the site. Your call. Which do you want before I run the eval?"

## The One Thing to Do First

Send the owner that choice and get an explicit answer before writing code or spending a cent. The owner's literal instruction is being declined, and the hard-to-redo paid eval has to run on whichever configuration they pick. While you wait, run the free offline worst-case render of partial R4 against 50,000 and 470,324 chars, plus the citation-integrity test, so the answer can be implemented the same day.

## Advisors

### The Contrarian

CONTRARIAN VERDICT: reject (b) and (c). Choose (a) plus (e)-lite, and keep S and the day cap exactly as they are.

What is wrong with the alternatives:
- (b) raises the real-money exposure ceiling on an anonymous public service, paid from a roughly $25 remaining budget. Every guard the owner pinned exists to stop this. You would also be editing production guards, the fly env, the RUNBOOK and the tests, all to buy a base-year id that the agent's own planner-computed lines already carry. That is a large, irreversible-feeling cost for marginal quality.
- (c) quietly weakens the abuse property. Seven addresses pausing the site for the day is cheaper to attack than eight, and Turnstile is not an identity system. You would be trading the core protection for a cosmetic citation.
- (d) is an unknown. Do not design caps around a number you have not measured. The paid eval is the one expensive, hard-to-redo step, so it must run against the final configuration. Never tune the caps after it.

Concrete recommendation:
- Agent: R6b also switches R4 off. Estimate stays about $1.016281, ceiling $1.02, margin $0.0037.
- That margin is thin. Before the eval, add a test that fails if the agent estimate exceeds $1.02. Do not spend a cent past it.
- S stays $1.32. The day cap stays $10. The eight-address rule is untouched.
- Hybrid: take the trimmed variant, N=6, abstract 120, allowance 46,000, estimate $0.743, margin $0.017. A $0.0009 margin is one rounding change away from breaking the ceiling.
- (e) is optional only. Add it only if the render stays under 470,324 chars, with a test proving it.

Tell the owner: "I kept the agent ceiling where it is. Raising it means raising your daily spend cap or letting fewer addresses pause the site, and both cost more than the one citation they would buy. The agent already cites both years on its own computed lines. If the paid eval shows a real gap, we can revisit it with measured costs."

### The First Principles Thinker

First principles: the goal is not to raise the agent ceiling. The goal is that a buyer demo is admitted whole while the site stays hard to pause and the owner's exposure stays at $10/day. Every option except (a) and (e) treats the caps as the thing to change, when the caps are the thing we are protecting. The agent is also the lowest-value place to spend the budget. Its own planner-computed lines already carry both years' ids, so R4 is mostly redundant there.

Recommendation: (e), with (a) as the fallback. Do not touch dayCap or S.

- Agent: keep R6b, and make R4 partial. Add the base-year id only on rows the question names plus the latest row. Cap the per-company allowance at 50,000 chars so the agent estimate stays at or below $1.02, as it is today. If partial R4 cannot be shown to fit 470,324 total, switch R4 off for the agent (option a). That costs the agent nothing it was not already giving itself.
- Hybrid: trim, don't raise. Use N=6 and abstract cut 120 with a 46,000 allowance, estimate about $0.743, margin $0.017. A $0.0009 margin is a bug waiting for the next rounding change. Hybrid carries R4 in full, since that is where the base-year citation matters.
- Caps: day cap $10, S $1.32, unchanged. Hybrid $0.743 gives S <= (10-0.743)/7 = $1.322, so rule (i') holds. S >= 0.30+1.016 = $1.316 holds too. The window is nonempty.
- Rejected: (b) puts real money and pinned production guards at risk for a cosmetic gain. (c) weakens the abuse floor from 8 addresses to 7 to buy one citation id.
- Don't wait on (d). The paid eval is the real measurement. Run it on the fitting configuration. Re-derive $0.30 afterwards, and only tighten.

Tell the owner: you raised the agent ceiling's intent, but the council found it unnecessary. The agent keeps today's ceiling and gets the prior-year id where it matters, with no extra daily exposure. Raising the cap would have cost real money and made the site easier to pause.

### The Expansionist

EXPANSIONIST VERDICT: choose (c) plus the 48,000 hybrid allowance, and raise the agent ceiling to $1.07. Do not touch the day cap.

Why this is the biggest upside. The agent ask is the feature buyers pay attention to. A full-fidelity agent answer, with base-year ids on every computed year-over-year line, is what makes the demo look institutional-grade. Dropping R4 (option a) or trimming it (option e) shrinks the product to save about $0.05 per ask. Option (b) is the only real money lever, and it moves the day cap, which is the one number that exposes the owner's own wallet. Leave it at $10.

Exact numbers:
- Hybrid: keep N=10 and abstracts at 200, with the allowance at 48,000 chars. The estimate is $0.759145. The margin is thin, but it is a worst-case ceiling that settles at real cost, so the real exposure is far lower. Trimming to N=6 degrades every hybrid answer, and hybrid is the most-used ask.
- Agent ceiling: $1.0693 (per-company allowance 56,500). Round the declared ceiling to $1.07.
- Rule (i') changes from eight addresses to seven: S <= (10 - 0.7591)/6 = 1.540.
- Per-address share: S = $1.38. The demo lower bound is 0.30 + 1.0693 = 1.3693, which leaves a $0.011 margin. S is still under the 1.540 upper bound. Do not move the production guard from 1.32 beyond 1.38.

Abuse cost: pausing live asks takes seven addresses instead of eight. Turnstile, not the address count, is the real gate, and the owner already accepts that one more address pauses a $10 demo for the day.

Do (d) afterwards. If the paid eval shows real per-ask costs are lower than $0.30 settled, tighten S back down. That is free upside.

Tell the owner: "You keep the full agent feature and the full hybrid context. The day cap stays at $10, so your maximum daily loss does not change. The only trade is that seven addresses, not eight, can pause the site for a day. Changing the S guard from 1.32 to 1.38 is a reviewed, test-pinned change. Run the paid eval after it lands."

### The Outsider

As an outsider, I see a plan where each number is pinned by another number, and the owner is spending real money to defend a detail that barely matters.

The detail is R4, the base-year id on every computed year-over-year line. The text itself says the agent's own planner lines already carry both years' ids, so R4 "matters less for the agent." Raising a spend ceiling to keep a feature that matters less is a bad trade. The owner's wording was "I raise its estimate ceiling." My reading is that the owner wants the agent feature kept, not R4 specifically. Option (a) keeps the agent and gives up almost nothing.

Choose (a): R6b also turns R4 off for the agent. The numbers:
- Agent ceiling stays at $1.02, with an estimate of $1.016281 and a margin of $0.0037.
- S stays at $1.32.
- Day cap stays at $10.
- No production guards change, and no pinned tests change.

For hybrid, take the trim: N from 10 to 6, abstract cut from 200 to 120, allowance 46,000 chars, estimate $0.743. The 48,000 option leaves $0.0009 of margin, which is one rounding error from breaking the ceiling. The trim leaves $0.017.

Do not take (b) or (c). Both loosen abuse resistance or raise the owner's exposure, and the owner has about $25 left in total. Do not take (e) either. A partial R4 adds a new rule to explain and test.

Before the paid eval, which can't be redone, do two things. First, confirm that all four estimates sit at or under their targets. Second, add an eval case that checks whether the agent answers year-over-year questions correctly without R4. If that case fails, there is real evidence for revisiting the decision, and (d) can wait for real per-ask costs.

What to tell the owner: "I kept the agent and raised nothing. The feature you wanted is intact. Only a prior-year id on single computed notes is dropped, and the agent's own lines already carry it. Raising the cap would have moved your hard guards and cost real money for little gain."

### The Executor

Executor verdict: ship option (e), partial R4 for the agent, and touch no production guards. Monday morning this is a half-day code change, and the paid eval can start the same afternoon.

Why: options (b) and (c) edit hard-coded production guards, the fly env, the RUNBOOK and the pinned tests. They also put real money and abuse resistance at stake for a feature the agent already carries in its own computed lines. Option (d) can't be done until the paid eval has run. Option (a) is the fallback, but it throws away R4 where it does matter.

Exact numbers:
- Day cap stays $10.00. S stays $1.32.
- Hybrid: take the trim, N=6, abstract cut 120, allowance 46,000 chars, estimate about $0.743. The $0.0009 margin at 48,000 would break on any unmeasured render. The trim gives $0.017 of margin and also tightens the (i') bound: (10 - 0.743)/7 = 1.322, which keeps S = 1.32 legal.
- Agent: R4 only on rows the question names plus the latest row. Cap the per-company allowance at 50,000 chars, render ceiling 470,324, estimate at or under $1.02 (measured $1.016281 without R4). If partial R4 still overflows, gate it by dropping the oldest noted rows first, not by raising the ceiling.
- Demo window: S >= 0.30 + 1.02 = 1.32, which just fits. Verify the 20-ask demo test passes unchanged.

Sequence: implement partial R4 test-first, re-measure the worst-case renders, and run the existing config tests. If the agent estimate exceeds $1.02, fall back to (a) for the agent. Then run the paid eval (about $3-10) and use the real per-ask costs to decide (d) later, as a separate reviewed change.

Tell the owner: "I raised the agent's ceiling as far as the caps safely allow, which is not at all, and I kept the feature. The agent gets the prior-year citation on the rows that matter. Raising the day cap to fund the rest would cost you real money and make the site easier to pause, for a gain the agent's own computed lines already cover. If the eval shows real asks cost less than $0.30, we can revisit the caps with data."

## Follow-ups

Added 2026-11-22 after the review of the M2a working tree. Owner instruction: do not over-TDD for now.

### F1. The per-anchor worst case is made realistic; the hybrid allowance moves from 46,000 to 48,000

The first worst-case fixture was too tame: real data rendered about 46,650 characters per anchor against the 46,000 allowance. The fixture
(`tests/test_serve_estimate.py`, `worst_case_retrieval` and `_metrics`) now uses 7-digit CIKs, 17-character TWD amounts, `:II.1A:` chunk
ids, two metric series whose priors are not positive (the longer `computed: n/m` notes), and evidence with `is_current` False on every
edge line, so every superseded mark renders. Measured through the real `build_blocks` / `render_prompt`:

| Case | Per company, two pairs | One pair |
|---|---|---|
| plain ask (quote lines and abstracts on) | 46,651 | 44,739 |
| agent, 5 pairs, R4 off | 49,475 (fits 50,000 with 525 to spare) | n/a |
| workspace (quote lines and abstracts off) | 43,759 | 41,847 |

`GRAPH_CHARS_PER_ANCHOR` is now 48,000 (46,651 rounded up to 47,000, plus a margin of 1,000). Hybrid estimate: $0.759145 (was $0.742825),
inside both the $0.76 target and the share window: `(10 - 0.759145) / 7 = 1.320122 >= 1.32 + 0.0001`. QUOTES_PER_ANCHOR stays at 6.
The agent allowance (50,000 per company, 470,324 in all) still holds; its measured worst case rose from 449,059 to 452,021 characters.

### F2. Workspace asks render without quote lines and rule abstracts; "eight addresses" holds for workspace again

With the F1 allowance a workspace ask would have cost $0.784 (and was $0.767884 before it), above the $0.76 that seven full shares leave, so
a workspace ask was paused with the agent's. `workspace.build_workspace_prompt` (used by the sync and async paths) now builds the SEC context
with `DETAIL_LINES_KEY: False`, the switch the agent uses: no R5/R6 quote or abstract lines. R2 ordering, R4 and the superseded marks stay.
The workspace estimate has its own allowance, `GRAPH_CHARS_PER_ANCHOR_WORKSPACE` = 45,000 (43,759 measured, rounded up to 44,000, plus
1,000). Workspace estimate: $0.759724 (target 0.76). Seven full shares plus a workspace ask: 9.24 + 0.759724 = 9.999724 <= 10, and
`tests/test_state_contract.py` asserts again that a workspace ask fits after seven full shares. Margin is thin (276 micro-dollars): any
growth of the workspace prompt template or context must re-measure.

### F3. `computed:` cannot be forged from untrusted text

`sanitise_untrusted` rewrites the `computed:` marker (case-insensitive, optional space before the colon) to `computed -`, so a filing quote
or a rule abstract cannot mimic a code-computed year-over-year figure that the verifier would use to ground an uncited percentage.
Test: `tests/test_context_sanitise.py` (a forged `computed: +40.0%` in an edge quote does not ground an uncited 40%).

### F4. Probe pins

`tools/probe/paced_asks.derive_ask_count` computes `floor(10,000,000 / hybrid)`: with $0.759145 that is 13 (it was 14 at $0.709573). The pins
in `tests/test_tools_probe_w1b.py` and `tests/test_tools_s7.py`, the registered W1c workload in `deploy/staging/windows.json` and the prose in
`tools/probe/README.md` follow the live computation. The recorded W1c boot files (2026-11-05) ran with 14 and are history; a re-run uses 13.

### F5. Worst-case fixture made real; edge-line allowance re-measured; the agent holds 4 pairs (2026-11-22)

F1 said the fixture used 13-digit TWD amounts, non-positive priors and a superseded mark on every edge line; it did not (12-digit USD amounts,
no non-positive series, marks on the quote edges only). It now does (`tests/test_serve_estimate.py`: `_metrics`, `_superseded`, and a test that
pins what the fixture builds). Measured through the real `build_blocks` / `render_prompt` / workspace path:

| Case | Per company | Allowance |
|---|---|---|
| plain ask, quote lines and abstracts on, 2 pairs (1 pair) | 47,505 (45,593) | 48,000 |
| workspace, quote lines and abstracts off, R4 on, 2 pairs (1 pair) | 44,613 (42,701) | 45,000 |
| agent, R4 off, detail lines off, 1 / 2 / 3 / 4 / 5 pairs | 45,930 / 47,842 / 43,496 / 49,856 / 49,987 | 50,000 |
| agent through the real merge (cap note included), cap 4 / cap 5 | 49,699 / 50,228 | 50,000 |
| longest RELATIONSHIPS line, 3 ids, 3 superseded marks | 268 (269 with newline) | `EDGE_LINE_CHARS` 280 (was 300) |

Settings kept: `QUOTES_PER_ANCHOR` 6, quote cap 240, abstract cut 120, mark `(superseded)`; none of them touches the workspace per-company
figure (it has no quote lines or abstracts) and the hybrid figure already rounds to 48,000 at the 500 step, so no rendered line changed and
`CONTEXT_VERSION` stays `m2a-1` (no fixture re-recorded). What brings every estimate under its target is the re-measured edge-line allowance
(150 lines x 20 characters = 3,000 characters per ask, 4 x 150 + 40 more for the agent). Two things were forced by the measurement:
the workspace figure (44,613) is above the 44,000 that $0.755 would need, so only the edge-line allowance could carry it; and 5 pairs through
the real merge (with its cap note) is 50,228 characters per company, over the 50,000 agent allowance, so `MAX_PAIRS_HELD_PER_COMPANY` is now 4
(the graph holds at most 4 consecutive comparisons of any company, AMD: five filings; nothing the data holds is cut). The agent ceiling is not
raised. Raising the cap back to 5 needs a 50,500 allowance (agent estimate about $1.0126, still inside the share window).

Final estimates (live models): hybrid $0.753229 (target 0.755), workspace $0.753808 (0.755), agent $1.008733 (1.0175), vector $0.266689. Margins:
`0.30 + agent = 1.308733 <= 1.32 - 0.0025`; `(10 - 0.753229) / 7 = 1.320967 >= 1.32 + 0.0005`; `7 x 1.32 + workspace = 9.993808 <= 10 - 0.0005`.
Pinned in `tests/test_m2a_spend_budget.py`. The probe ask count `floor(10 / 0.753229)` is still 13. Other pins moved: `tests/test_serve_estimate.py`,
`tests/test_state_contract.py`, `tests/test_tools_probe_w1b.py`, `tests/test_tools_s7.py`, `deploy/staging/windows.json`, `tools/probe/README.md`,
`docs/RUNBOOK.md`, `src/semigraph/config.py` (share-window rationale).

### F6. Paid-run guard and the default anchor's quote lines

`eval-deployed` refuses to resume a runs file whose rows carry no string `context` (those rows were recorded before the context was saved and
cannot be re-judged against what the model saw); the operator passes a new `--runs-file`. When a question names no company, retrieval anchors
on the default company with `anchors = {}`; `build_blocks` now treats that default company as an anchor when `anchor_defaulted`, inside the same
per-anchor quote cap.

### F6. After the first paid eval: quote relevance, base-year rule, checker (2026-11-22)

The first paid eval (`artifacts/eval_report.m2a-deployed.json`) missed three section-6 rows; each got one fix. R6: quote lines are chosen per
anchor by relevance to the question (shared content words, the relation the question names, the other endpoint), ties in query order, so the
cap of 6 no longer cuts a DEPENDS_ON quote behind six COMPETES_WITH ones; `CONTEXT_VERSION` is `m2a-2`. OD4: one rule in `answer.txt` and
`answer_workspace.txt` asks that a change taken from a "computed:" line cite both ids printed on it. The templates are now 9,508 and 10,608
characters, so `ANSWER_TEMPLATE_CHARS` is 9,600 and `WORKSPACE_TEMPLATE_CHARS` is 10,700 (the rule adds 105 characters to each).
Final estimates: hybrid $0.753229 (target 0.755), workspace $0.753808 (0.755), agent $1.008733 (1.0175), vector $0.266689. Margins:
`0.30 + agent = 1.308733` (11,267 micro-dollars under 1.32), `(10 - 0.753229) / 7 = 1.320967` (967 over), `7 x 1.32 + workspace = 9.993808`.
The probe ask count `floor(10 / 0.753229)` is still 13. The examples fingerprint changed; the seeded examples are regenerated after the next paid run.
