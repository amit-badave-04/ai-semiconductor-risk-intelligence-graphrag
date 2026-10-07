# Expansionist

**Choose (b), and treat it as a reusable realism instrument rather than a workaround.**

The decisive point is that (a) makes the test easier than production in a way nobody will notice. Skipping answer-cache writes removes the write path from the load, and production does that write on every live ask. (a) also puts two new switched-off code paths into the production image for a one-off test. (b) keeps every production code path live and unchanged, so the cache-write cost, the cache growth and the LRU churn all show up in the measurement. The staging cache growth of about 10k rows per hour is a bonus. It lets you check the answer-cache store at production-like write rates for free.

The bigger upside is to make the salt a seeded, versioned harness component.
- **Rotate natural qualifiers.** Instead of one fixed suffix style, rotate several (" (ref N)", a filing-year clause, a company-context clause). That adds retrieval diversity close to option (c), at zero LLM spend.
- **Reuse it for M5b.** The same harness gives you a cost model for cache growth and for embedding CPU per unique ask.

Safeguards, all of which are diagnostics added on top of the pre-registered gates:
1. **Pre-run divergence check.** Measure overlap@k between salted and unsalted retrieval across all 300 questions. If the median overlap is high, the retrieval distribution is preserved. If not, change the salt style.
2. **Validation check.** Confirm the salt passes question validation, company and alias detection, and the length limits.
3. **Cache and LRU assertions from the ledger.** Check that the live answer-cache hit rate is ~0%, that the embedding LRU hit rate is ~0%, and that the cached stream hits only the pre-cached, unsalted example set.
4. **Embedding cost check.** Measure the embedding cost per salted ask against an unsalted cold baseline. The salt adds tokens, so it should be equal or slightly harder, which is the conservative direction.
5. **Reproducibility.** Use a fixed seed, and log the salt scheme and the divergence number in the S2 report.
6. **Wording.** Cite the result as "1,000 virtual users, salted unique questions, mocked LLM".
