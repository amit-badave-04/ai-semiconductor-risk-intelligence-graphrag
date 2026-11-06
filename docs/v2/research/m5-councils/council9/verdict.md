# Council 9: chairman's verdict (Opus)

## Where the Council Agrees

- **Do not spend $51.53 a month on option C now.** It is a recurring cost for a rarely used feature, and the owner has not seen data that justifies it.
- **Do not build option D during M5b.** A permanent worker is architecture work while M5b is in flight. An external embedding API would send private PDFs off the box and would force a re-index, because the index is locked to this embedder.
- **Two ONNX threads will not help.** The CPU quota is pooled across vCPUs, so a second thread gets nothing extra once the burst is spent.
- **2,100 s is better than the live 1,200 s.** The code is already built, so shipping it costs $0. The question is what ships with it.
- **No ceiling number fixes this alone.** The throttle state is random and nobody controls it. Observed run times range from 388 s to an estimated 2,553 s, so another ceiling sized from one more sample can fail the same way.
- **Read the per-stage timing line from real uploads before any paid or architectural decision.**
- All five peer reviews ranked the Contrarian's plan strongest. Its parts were:
  - record W1d as a failed gate;
  - abort early when the projected finish will not fit;
  - lower the cap to a number derived from the measured rate;
  - keep 2,100 s and the day rule;
  - defer C and D until data shows they are needed.

## Where the Council Clashes

**1. Lower the document cap, or keep 16,000 tokens.**
- For a cut: the First Principles Thinker proposed about 7,700 tokens and the Contrarian about 10,000. The Expansionist, the Outsider and the Executor want to keep 16,000.
- **Ruling: cut to 10,000.** At the worst observed rate (5.54 tokens/s), 2,100 s covers about 11,634 tokens. A 10,000-token upload then needs about 1,805 s, so the worst draw seen so far succeeds.
- The 7,700 figure puts a 1.5x margin on top of a rate that is already the worst observed. That is more caution than one sample supports.
- The cut is one config value, so it can be raised again when the data allows.

**2. Raise the ceiling to 3,600 s (option A), or keep 2,100 s.**
- **Ruling: keep 2,100 s.** Option A has four problems:
  - it is a 1.25x margin on a single sample;
  - it holds the single upload slot for up to an hour;
  - for that hour it uses up the shared CPU quota the paid asks need (nice 10 wins inside the machine, but the quota limit sits above it);
  - it still fails at the end, after the visitor has left.
- The Executor's claim of "no new code" also contradicts its own progress-feed step.

**3. Ship 2,100 s alone now, or only together with other fixes.**
- **Ruling:** ship the ceiling and the new cap together, as config only, once the owner approves the amendment. The early-abort code follows quickly.
- 2,100 s with a 30-page public promise must never be described as fixed.

**4. A fixed ceiling, or a timeout based on progress or stalls.**
- The Outsider and the Expansionist favoured the progress-based rule.
- **Ruling: keep a fixed hard ceiling.** The validator needs one, and a stall-only rule lets a slow upload hold the slot and starve asks with no end.
- Keep the useful half of their idea: a re-projected abort (see The Recommendation).

**5. The day rule (option F).**
- The Outsider wanted to drop it. **Ruling: keep it.** 2,100 × 40 = 84,000, which is within 86,400, so no change is needed.

**6. What W1d means.**
- The Executor called it evidence and amended around it. The Contrarian called it a failed gate.
- **Ruling: both.** It is a failed pre-registered gate, recorded as FAIL. Its numbers then size the amendment, and the owner must re-approve that amendment.

## Blind Spots the Council Caught

These are checks to run, not conclusions.

- **W1d may not represent production.** The 600 s burn drained the burst balance on purpose, so W1d measured the floor, not a typical production run. A production machine that sits idle between rare uploads probably starts with burst credit, which would match W1c's 388 to 1,174 s. Still, design for the floor.
- **The embed got about half the burn's share (5.5% vs 10.1%), and nobody explained why.** Two $0 checks would tell:
  - In the W1d logs, did the burn process exit before the embed started? A burn that was still running would roughly halve the embed's share.
  - Log the machine's own CPU counters (`cpu.stat`: CPU time used vs. time throttled) in the per-stage timing line.

  If the embed got about 10% of the CPU but produced only 5.5% worth of tokens, the fix is to cut the work per token. If it got only 5.5% of the CPU, the limit is the quota. If the halving turns out to be a test artifact, raise the cap back toward 16,000.
- **Work per token was never tried.**
  - Length-sorted batching with dynamic padding leaves the vectors unchanged and is the first lever to check.
  - Moving to q8 or int8 is an index change, not a free speedup. The index is locked to this embedder and fp32 was an earlier owner decision, so leave it alone for now.
- **A single slow-but-alive upload blocks every other visitor**, because there is one upload slot.
- **A timed-out upload may count against the visitor's daily allowance.** It should not.
- **Nobody measured ask latency while a throttled upload runs.**
- **An early rate can mislead.** It looks good while the burst lasts and gets worse once the burst runs out.
- **A performance-1x machine with 4 GB was never priced.** The embedder uses one thread, so it may cost much less than performance-2x. Price it before any decision on option C.
- **A per-upload worker is not free.** The Expansionist's $0.01-per-upload figure is unverified. A short-lived performance machine billed per second is the right post-M5b candidate, but it brings real costs:
  - a cold start on every upload;
  - a new trust boundary for private PDFs;
  - writing the vectors back from another machine;
  - a variable cost the owner must approve.
- **The amended spec needs the owner's re-approval** before anything deploys.

## The Recommendation

This is an amendment for the owner to approve or reject, in this order.

1. **Today, $0.**
   - Record W1d as FAIL in the internal log and in the spec's Status block.
   - Check the W1d logs for the burn/embed overlap.
   - Write the amendment:
     - ceiling 2,100 s and 40 uploads a day, both unchanged (84,000 ≤ 86,400);
     - `max_upload_tokens` lowered from 16,000 to 10,000, enforced as the binding check, with a clear rejection before any work starts;
     - a page figure that stays true for dense PDFs (about 18 to 20 pages).

     For reference: 16,000 tokens over 30 pages is about 533 tokens per page, so 10,000 tokens is about 18.75 pages at that density and about 21 at the fixture's density.
2. **Once the owner approves, deploy the already-built 2,100 s code with the new cap as config.**
   - Public copy states the limit as a plain fact ("PDFs up to about 20 pages").
   - Internal docs record the W1d numbers, the 30-page throttle failure and the floor rate.
3. **Next, a small code change through the normal SDD path: the projected-finish abort.**
   - After every chunk, re-project the finish time from the measured rate. Abort as soon as the projected finish exceeds the ceiling.
   - On abort, free the slot and do not charge the visitor's daily allowance. Show a neutral "busy right now, try again shortly" message.
   - Add `cpu.stat` to the per-stage timing line.
   - Tests: a slow rate aborts within a few chunks; burst-then-throttle aborts mid-run; a normal rate completes.
4. **In M5b:** the upload view shows stage progress (parse, chunk, embed n/N) and lets the visitor leave and come back. This is frontend scope, not new backend architecture.
5. **Measure over the first 10 or so real uploads:** the timing line, the CPU counters, ask latency during uploads and the abort rate.
6. **Tripwires, decided by the owner with data:**
   - If more than 1 in 10 uploads aborts, or ask latency degrades: price performance-1x 4 GB against performance-2x, and after M5b look at the per-second worker together with saving finished chunks so an upload can resume.
   - If the data is better than W1d (an overlap artifact, or banked burst credit): raise the cap in steps.
   - After M5b, check length-sorted padding.
7. **Do not do:** option A, option F, option C now, option D now, an external embedding API, two threads, or a stall-only timeout.

**Fallback if the owner rejects the cap cut:** ship 2,100 s only together with the projected-finish abort, never alone. Stop claiming 30 pages publicly, because on a throttled machine that claim is false.

## The One Thing to Do First

Write and approve or reject the one-page spec amendment. It records W1d as FAIL and keeps 2,100 s and 40 a day. It lowers the cap to 10,000 tokens (about 20 pages, enforced up front). It adds the projected-finish abort that re-projects after every chunk.
