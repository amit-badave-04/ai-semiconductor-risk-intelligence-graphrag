# Council 11: chairman's verdict (2026-11-10)

The chairman (model: opus) saw the question, the five named responses and the five peer reviews. The council advises; the
owner decides.

## Where the Council Agrees

All five chose B. I looked at the first screen myself and agree with the diagnosis. Under the wordmark it shows "9 filings pending", then "Ask", then "Benchmark questions (instant, cached)", then a wall of long revenue questions. Nothing says what the site is, there is no box to type in, and nothing says it is free. Three newcomer readings scored it 2, 3 and 2, which fails the owner's own bar ("a noob quickly grasps it"). The fix is wording and order, so it is cheap.

The advisors also agree on two more points:
- A11 (the NVDA screen-reader pass) and B6 (the owner's live click-through) were never done by a person, so they must not be recorded as passed.
- A 162-second rollback does not undo a buyer's first impression.

## Where the Council Clashes

- **A11 and B6 before or after the flip.** The Contrarian wants both done or explicitly waived first. First Principles and the Expansionist accept "open, after the flip". The Executor wants B6 first and lets NVDA wait. I side with the Executor. B6 covers the paid question, the workspace and the upload, none of which anyone has run live. A11 is a known gap that axe and the keyboard pass partly cover.
- **Retest bar.** The Contrarian asks for a score of 4 or more, First Principles for an average of 3.5, and three advisors ask for no retest at all. A fix nobody checks is an assumption.
- **The Expansionist's ideas** (leave REFUSAL and MISATTRIBUTION as they are; send the walkthrough to buyers) were rejected by every reviewer, and I agree.

## Blind Spots the Council Caught

- **The newcomer scores come from agents, not people.** An agent re-run would likely approve its own fix. One or two real people looking for 10 minutes is the better test.
- **D1, X4 and the 2 missing examples are not caused by the new frontend.** They are the same on the old page, so they hurt credibility but should not block the flip. The first screen and the Company page are new exposure.
- **Hiding beats fixing.** Showing the Company tab with a "beta" label, or hiding it, costs less than fixing every defect before the flip.
- **"Free" must be true before the site says it.** The paid-question flow has never run live. Confirm what a visitor actually pays before writing "free".
- **The edits change the page.** The 123 browser tests, axe and the containment check need a fresh run, and the change must be live on /v2/ before the flip.

## The Recommendation

**B. Flip after this list, which should take about a day.**

The build can do:
1. Add a two-sentence intro and a "free to use" line, once the owner has confirmed it is true (1 hour).
2. Put the question box above the examples, show 6 examples, and add a "show all" control (2 hours).
3. Reword the insider labels: "Benchmark...cached", "served from cache", "numbers not checked", "filings pending", REFUSAL, MISATTRIBUTION (1 hour).
4. Show the Company tab with a "beta" label, or hide it. Format the numbers as $215.9B, sort newest first, and hide the relationship rows that look reversed (2-3 hours).
5. Re-run the full test suite and axe, deploy to /v2/, and re-run the three agent readings, aiming for 4 or more (1 hour).
6. After the flip: fix or remove D1 and X4, and restore the 2 missing examples (2 hours).

The owner must do:
1. The B6 click-through on the live /v2/ after step 5: one paid question, one workspace and one upload, written down. This also confirms the "free" wording (30 minutes).
2. Show the first screen to one or two real people who know nothing about the project and write down what they say (15 minutes).
3. Record A11 as "not done, waived until <date>" (5 minutes).

**The owner's written sign-off may claim** that the automated gates are green, the agent walkthrough and audit were done, and B6 was done by the owner on a date. It may also claim the first screen was retested.

**It may not claim** that A11 passed, that buyers gave feedback (P8 was replaced and is still pending), or that newcomers understand the site, unless real people confirmed it.

The council advises; the owner decides.

## The One Thing to Do First

Add the two-sentence intro and move the question box above the examples, then put the new first screen in front of one real person who has never seen the site.
