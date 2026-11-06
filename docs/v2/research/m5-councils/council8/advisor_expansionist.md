# Expansionist

**Expansionist view: finish W1b as a pair of measurements, then turn the timeout into a finding and a feature.**

**1. Don't do (c), and don't loop on (a).** An INVALID boot is invalid by the driver's own pre-written rules. Counting it would be exactly the post-hoc relabelling the gate forbids. Re-running unchanged (a) is also pointless, because two identical failures already say the workload can't complete on this machine.

**2. Record W1b-as-written honestly.** The result is "boot 1 valid and PASS, boots 2a/2b INVALID, boot 3 not run." Report the four peaks as supplementary stress evidence: 35-49 MB under the limit across 118-154 saturating asks. That is the hardest memory test the site will ever see, and it held. State plainly that it is not the gate verdict.

**3. Run a new, separately named window, W1c, with a pre-declared rule.** Use the M4 G4-matched load (paced asks) on 3 fresh boots, not just boots 2 and 3. Mixing workloads across boots weakens the claim, and it costs cents. Write the PASS criteria and the decision rule into the repo before running. The justification is that the saturated reading was the driver author's interpretation, not what the live site does.

**4. Add a q8 control arm under the same saturated load.** This is the part everyone is underweighting. The claim that the timeout comes from the throttled machine and not from fp32 is currently an argument. A paired run makes it a measurement. fp32 is 3.6x cheaper per query embed, so it may well show fp32 degrading less than q8. That would turn an awkward finding into a point in favour of adopting fp32.

**5. Tell the owner now.** An unauthenticated visitor can keep 2 asks in flight and starve every upload past its timeout. That is a cheap denial of service on a public demo, and it exists today with q8. Fix it as its own spec, not inside W1b. The fix is to give upload passage embeds priority over query embeds, or to run them in separate pools. It is also a differentiator, because "uploads stay responsive under load" is a story worth telling.

**6. Keep the saturation profile as a permanent regression harness.** It will also expose the 15% post-burst throttle, which is relevant to the choice of machine class.
