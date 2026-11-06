# First Principles Thinker

**First principles: W1b exists to answer one question. Does fp32's peak memory stay safely under 2.4 GB on this machine during the heaviest upload the site can realistically face? Nothing in the three attempts changes that question.**

1. **Reject (c).** Counting INVALID boots as valid after seeing their numbers breaks a validity rule that was written before any run. It is the textbook way to gate-shop. The upload never finished, so the last passages were never embedded, and the memory peak may not have been reached.
2. **Reject (a).** Re-running an unchanged workload that has now failed twice for a deterministic reason (CPU starvation) tests nothing.
3. **Reject (d) inside W1b.** Changing the timeout or the scheduling changes the thing being measured.
4. **Do (b), with the deviation pre-registered.**
   - Write down the new workload, 5 asks during the upload, before running it. The anchor is M4 G4, an existing reference that was not chosen from W1b's memory results.
   - Boot 1 stands as a valid saturated pass.
   - Run boots 2 and 3 on the G4-matched workload.
   - Report the saturated runs as supplemental stress evidence: 2,364 MB peak, no OOM, upload timed out.
   - State plainly in the record that the literal reading of "live caps" produced 1 valid boot and 2 invalid boots, and that the workload was then changed.
   - Because the builder never waives a gate, the owner approves the changed workload before it runs.
5. **The thin margin matters more than the boot count.** All four boots peaked within 35-49 MB (1.5-2%) of the limit, whatever the ask load. Passing by 1.5% is weak evidence of safety, so the owner should see the margin itself, not just PASS.
6. **The timeout is a finding the owner must hear before any deploy.** It is independent of fp32, because q8 starves the upload worse. It is also a cheap denial-of-service: one visitor who keeps 2 asks in flight can kill everyone's uploads on a machine the owner pays for.
   - Do not fix it inside W1b. Give it its own spec, with a negative test.
   - Candidate fixes are giving upload embeds priority over query embeds, or making the timeout depend on progress instead of wall-clock time.
   - Optionally spend cents on one saturated q8 boot, so "q8 is worse" is measured rather than inferred.
