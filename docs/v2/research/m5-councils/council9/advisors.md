# Council 9: advisor responses

## The Contrarian

**Every option sizes a number to a distribution nobody controls.** The 12% share assumption was already wrong once: the embed got 5.5%, not 12%, and run times have spanned 388 to 2,553 s. A third ceiling set from one more sample will fail the same way. The pre-registered gate failed, so shipping 2,100 s (E) and calling it fixed waives your own gate.

**What's wrong with each option**
- **A (3,600 s):** An hour-long upload is a failure in practice, because the visitor has left. It is also worse for asks. Nice 10 wins the scheduler but not the hypervisor throttle, so the upload drains the same pooled quota the paid asks need. A throttled upload degrades every ask for that hour.
- **E and A:** Both fail at the END. A doomed upload burns the full 35 to 60 minutes of throttled CPU before telling the visitor anything.
- **C, D:** C is $51.53 a month for a rare feature. D is architecture work during M5b. Neither is justified yet.
- **F:** The day rule is a bound on how long one upload can hold the slot. F deletes it to make a number fit.

**Order**
1. Record W1d as a failed gate, honestly, in the internal docs.
2. Add admission control. Measure tokens/s over the first 3 chunks, project the finish time, and abort in under a minute if it won't fit. Show a neutral message such as "processing is slow right now, try again shortly or use a shorter PDF."
3. Cut the cap to about 10,000 tokens (about 21 pages). At 5.54 tok/s, 2,100 s covers about 11,600 tokens, so this keeps roughly 15% margin even on the worst observed draw. It is B, but deterministic, and public copy can state the new cap as a plain fact.
4. Keep 2,100 s, which passes the validator at 40 a day. Deploy it with steps 2 and 3, not alone.
5. Read the per-stage timing from real uploads for a few weeks. Revisit C or D only if abort rates are material.

n=1 on the throttle state isn't a floor either, which is why step 2 matters more than step 3.

## The First Principles Thinker

The ceiling is the wrong variable. The real question is what upload size you can honestly promise at the worst throughput you have measured. Fly's CPU draw is a lottery you don't control, so size the product to the floor, not the average.

**Measured floor:** 5.5 tok/s. With the spec's own 1.5x margin, that gives 5.5 × 2,100 / 1.5 ≈ 7,700 tokens. That is option B's number, and it follows from your rule and your data, not from a guess. A 16,000-token cap is a bet that the CPU draw will be good, and W1d just showed the bet can lose.

A silent failure on a bad draw is worse than a smaller cap. A visitor who waits 40 minutes for nothing is worse off than one told "up to about 15 pages" at the start. Nobody waits 43 to 60 minutes for a demo, so options A and F buy you a timeout that no user will outlast.

**Order:**
1. Deploy the 2,100 s ceiling now. It is built, approved and strictly better than 1,200 s.
2. In the same deploy, set `max_upload_tokens` to about 8,000. Keep the day rule, because 40 × 2,100 s fits the invariant and the invariant is correct. Write the cap as a strength in public copy ("up to about 15 pages"). Record the floor evidence and the 30-page limitation internally.
3. Make failure honest and early. Check the rate after the first few chunks. If the projected time exceeds the ceiling, abort with a clear message and free the slot.
4. Defer C and D. Paying $51 a month for a feature that is rarely used is the wrong trade. Read the per-stage timing line from real uploads first.
5. If demand for larger documents shows up after M5b, build an ephemeral performance-CPU machine per upload. That bills per second, so it costs cents, not $32 a month.

Do not touch resumable parts or a progress-based timeout now. They are architecture work solving a problem you can currently remove with one config value.

## The Expansionist

**Don't shrink the product. Ship E today, then turn the throttle problem into a feature.**

1. **Deploy 2,100 s now.** It costs nothing and is strictly better than 1,200 s. Record the W1d result and the per-stage timing line in the internal docs only. Skip A, B and F. B halves the document size to fix a CPU-lottery problem. A makes a user wait an hour, which is worse than a clean failure, because it fails silently.

2. **The upside is in D, priced per use, not C or D as a flat $32 a month.** A stopped Fly Machine costs only a few cents a month for its rootfs. On each upload, start an ephemeral performance-class worker from the same image through the Machines API, embed on a full core, write the vectors back, and stop it. Embedding takes about 3-4 minutes, which is roughly $0.01 per upload at performance-2x rates. Even at the 40-a-day cap that is under $15 a month, and realistically it is cents. This is variable spend with a hard ceiling, so the owner can approve it as a capped line item. Private documents stay inside Fly.

3. **That worker is worth more than the fix.** It can:
   - lift the public limit from 30 pages to 100 or more, which is a strength you can state publicly;
   - take over the big re-index that the schema-locked embedder otherwise makes painful;
   - remove the upload's CPU contention with asks;
   - be reused for any later batch job.

4. **Add a stall-based timeout (G).** Give a progress-based liveness rule, such as chunk throughput over a rolling window, a generous hard cap, and a daily compute budget in place of the day rule. This works on the shared-CPU path and is free.

**Order.** Deploy E now and watch the timing line. Finish M5b. Then spend one small, capped spike on the ephemeral worker. Do it after M5b, not during it, because the worker is a hand-off design problem and should not compete with the frontend work.

## The Outsider

The strangest thing here is that a visitor uploads a 30-page PDF and the demo may spend 35 to 48 minutes on it. Nobody waits that long. The real question is what the visitor sees, not which timeout number is right. A 50-of-60-chunks failure that throws the work away is the worst outcome on the table.

**Order I'd go in:**

1. **Ship E now.** 2,100 s beats 1,200 s, it costs $0, and it is already built. Don't hold it back for a perfect answer.

2. **Fix the thrown-away work.** At the ceiling, 50 chunks were done and then discarded. Save each finished chunk's embedding and let the upload resume or retry from there. This is a small change. It makes the CPU lottery mostly harmless, because a slow draw becomes a longer wait instead of a failure.

3. **Replace the fixed ceiling with "fail only if no chunk finishes in N seconds"** (G). Add a generous hard cap. The real failure is a stall, not slowness.

4. **Drop the day rule (F).** The rule `ceiling x uploads/day <= 86,400` guards against 40 worst-case uploads running back to back, one at a time, all day. With rare uploads that cannot happen. If you need a guard, use a rule about how long the machine's CPU may be busy per day, not this arithmetic.

5. **Tell the visitor the truth in the interface.** Show progress and "this can take a while, we'll keep it." Don't promise a duration in public copy.

6. **Don't spend $51 a month (C) on a rare feature.** If you ever need speed, look at a performance machine started for each upload and billed by the second. It costs pennies when uploads are rare, and it is far cheaper than a permanent worker.

Keep B and A in reserve. Cutting the document size or the daily cap is a product cut. Make it only if the per-stage timing from real uploads shows the problem is real.

## The Executor

**Do A, today, and nothing else is on the critical path.**

W1d already gave you the number: the worst observed rate is 5.54 tok/s, so a max-size upload needs about 2,887 s. A ceiling of 3,600 s clears that with a 1.25x margin. It costs $0 and needs no new code, because 3,600 x 24 = 86,400 exactly satisfies the validator. Nobody uploads 24 documents a day to a demo where uploads are rare. Cutting 40 to 24 costs you nothing real, while option B halves a headline capability and F deletes a safety invariant.

**Monday order:**

1. **Amend the spec and config (30 minutes).** Set ceiling 3,600 and cap 24. The spec is owner-approved, so write the amendment as a one-line Status entry citing the W1d evidence. Run the validator tests, deploy, and do one prod smoke upload. Don't pay for another staging boot, because W1d is your evidence.
2. **Make failure loud, not silent.** On timeout, return a clear "didn't finish, try again" state and don't burn the visitor's daily slot. Hand the M5b frontend a stage-progress feed (parse, chunk, embed n/60), so a long wait looks like progress. That is public copy about a strength, with no limits disclosed.
3. **Ship the per-stage timing line (option E's instrumentation) and read it after about 10 real uploads.** Write the "worst case about 48 minutes on a throttled machine" caveat in the internal docs only.
4. **Set a tripwire for C.** Move to performance-2x only if more than 1 in 10 uploads hit the ceiling, or ask latency complaints appear. Then it is a data-backed $51/month decision, not a guess.

**Skip for now:** D (an architecture change mid-M5b, and private documents leaving the box), progress-based timeouts, resumable parts, and two threads (the quota is pooled, so it won't help). Revisit resumability only if the tripwire fires and you still want to avoid C.
