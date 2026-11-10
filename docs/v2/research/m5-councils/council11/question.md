# Council 11: may the new frontend become the default? (2026-11-10)

## The decision

semigraph is a research site over the SEC filings of 13 chip and AI companies: a visitor asks a question and gets an answer
whose citations open the filing passage, the reported number or the Federal Register rule behind it. A new frontend (React)
has been live since 2026-11-10 at `/v2/` (hidden from search engines); the old single-page site is still what visitors get
at `/`. The last step of milestone M5b is to switch one flag (`UI_V2`) so that the new frontend is served at `/` and the old
page moves to `/legacy`.

**Question: should the owner sign off now that the new site can become the default, and if not now, what exactly must
happen first?** Choose one: (A) flip now, as it is; (B) flip after a short, named list of changes or checks; (C) do not
flip until something larger is done (say what).

The owner's own bar, in their words: "make sure even a noob should be able to quickly grasp this website." The owner
expects the answer to be mostly yes. Do not let that expectation move you.

## What is known

Cost of being wrong: low to reverse technically. A rollback is one same-image deploy of about 162 seconds (drilled on
2026-11-10), and the old page stays at `/legacy`. The cost that cannot be rolled back is a first impression on a real
visitor or a buyer, and the owner's public repository links to the site.

Engineering state: all automated gates are green (1,138 unit tests, 123 browser tests with axe in both colour schemes, the
image containment check on the live machine, no Critical or High security finding open).

Evidence gathered today on the live preview by the build agent, at the owner's request (the owner asked the agent to run
their walkthrough and accessibility pass for them):

1. **Persona walkthrough** (`docs/v2/M5B_PERSONA_WALKTHROUGH_RESULTS.md`). A script clicked 29 free cached example
   questions and every citation; model agents scored the results as seven buyer personas. Averages: equity analyst 3.83,
   portfolio risk 3.6, quant 2.75 (3.0 after a re-check), event-driven 3.67, credit 4.0, strategy and compliance 3.33, data
   vendor 4.0 (5 = would use as is, 3 = useful but must double-check). No task scored 1. "Would use": one yes, six maybe.
   An independent audit found every checkable number equal to its source, every change claim hedged, no rule presented as a
   company statement, and no invented figure in the refusals.
2. **Three first-time-visitor readings** (agents with no knowledge of the project, shown screenshots and page text): grasp
   scores 2, 3 and 2 out of 5 (5 = understood within about 10 seconds; 3 = worked it out after a minute). All three said:
   no sentence on the first screen says what the site is (the old page has an introduction paragraph; the new one has it
   only on the Method page); the box for typing your own question is below 51 long example questions, so it is not visible
   on the first screen (the old page has the same order); the cost wording never says the visitor pays nothing; and the
   wording is insider language ("Benchmark questions (instant, cached)", "served from cache - no model call", "numbers not
   checked", "filings pending", REFUSAL, MISATTRIBUTION). They liked the clean layout, the instant answer from one click and
   the evidence panel.
3. **Company pages** (new in this frontend; the old page has none): the Metrics table shows raw integers (215938000000),
   internal metric names (rnd, net_income) and, for Nvidia and Amazon, capex rows from 2011-2012 and 2015-2016 at the top;
   some relationship rows look reversed ("Apple CUSTOMER_OF Alphabet" over a passage about Google paying Apple); "Federal
   Register rules: none" for ASML with no explanation; the risk comparison is "not available" for Intel and ASML.
4. **Saved answers** (the same on the old page): two saved answers contradict their own cited passage on a detail (D1, X4),
   two percentage answers cannot be reproduced from what the page shows, and 2 of the 53 packaged examples are missing
   from the live list.
5. **Accessibility** (`docs/v2/M5B_A11Y_MANUAL_PASS.md`): a keyboard and accessibility-tree pass of the five live pages: 40
   rows pass, 0 fail, 10 not tested; axe finds 0 violations on the live pages in both schemes. **The screen-reader (NVDA)
   pass that the spec requires from a person (A11) was not done**: NVDA is not installed on the PC and an agent cannot
   listen.
6. **Never exercised on the live site through the new frontend, by anyone:** a live paid question, creating a workspace,
   an upload. Each needs a person to pass the bot check; the agent does not pass bot checks. These flows are covered by
   automated browser tests against a fixture server only. The spec's gate B6 asks for the owner's own click-through of the
   live `/v2/` confirmed in writing.
7. Gate P8 asked for named buyers' feedback; the owner replaced it on 2026-11-08 with an owner-run persona walkthrough
   ("real-buyer feedback pending"). Today's run was by an agent, not the owner and not buyers.

## What is at stake

The owner wants M5b closed; it has taken long. A wrong "yes" puts a first screen in front of buyers that newcomers did not
understand, and closes two gates (A11, B6) on evidence that a person never produced. A wrong "no" keeps a better-built,
accessible frontend behind a preview path for no good reason.
