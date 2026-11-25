export const meta = {
  name: 'm2-bench-drafting',
  description: 'M2-Bench: blind question drafting per type (Sonnet, filing passages only) and an independent Opus check per type',
  phases: [
    { title: 'Draft', detail: 'one blind Sonnet drafter per type (A-F)' },
    { title: 'Check', detail: 'one Opus checker per type', model: 'opus' },
  ],
}
const DIR = '<scratch>/m2_blind'
const RULES = {
  A: 'Type A, filer-own passage: the passage is the filing company speaking about itself ("we", "our") without naming itself. Write a question about that company (NAME the company in the question) that this passage answers. Do NOT copy the passage sentence: paraphrase.',
  B: 'Type B, exact term: the passage contains a specific term, identifier or product name (e.g. an ECCN like 3A090, a product such as CoWoS or H200, a named rule). Write a question that USES that exact term and names the company. Lexical overlap is intended here.',
  C: 'Type C, number in prose: the passage states a figure in text (dollars, percent, units). Write a question whose answer is that figure; name the company and the period. Do not copy the sentence.',
  D: 'Type D, two companies: the item has TWO passages from two different companies on a shared topic. Write ONE comparison question naming BOTH companies that needs both passages. Give one quote from EACH passage (quotes[0] from passage 1, quotes[1] from passage 2). Do not copy sentences.',
  E: 'Type E, long-tail risk: the passage is one specific risk factor. Write a question about that specific risk (not a generic one), naming the company. Paraphrase; do not copy the sentence.',
  F: 'Type F, period-specific: the passage is tied to a specific quarter or fiscal year. Write a question that names the company AND that period and is answered by this passage. Paraphrase.',
}
const OUT = {
  type: 'object', properties: { items: { type: 'array', items: { type: 'object', properties: {
    ref: { type: 'string' }, skip: { type: 'boolean' }, skip_reason: { type: 'string' },
    question: { type: 'string' }, quotes: { type: 'array', items: { type: 'string' } }, answer: { type: 'string' },
  }, required: ['ref', 'skip'] } } }, required: ['items'],
}
const CHECKED = {
  type: 'object', properties: { items: { type: 'array', items: { type: 'object', properties: {
    ref: { type: 'string' }, verdict: { type: 'string', enum: ['accept', 'fix', 'reject'] }, reason: { type: 'string' },
    question: { type: 'string' }, quotes: { type: 'array', items: { type: 'string' } }, answer: { type: 'string' },
  }, required: ['ref', 'verdict', 'reason'] } } }, required: ['items'],
}
const BLIND = `BLINDNESS RULES (strict): your ONLY input is the file named below. Use the Read tool on that ONE file and nothing else.
Do not search, list, grep or open any other file or directory, do not run commands, do not browse. You know nothing about the
system that will be tested and must not try to find out.`
const COMMON = `Each item has a ref and one passage (two for type D) from a US SEC filing: company ticker, form, filing date, section, text.
For each item write ONE benchmark question for a retrieval test over SEC filings:
- self-contained, specific, answerable from the passage(s), with a short factual answer;
- names the company (or both companies for type D) by its common name, not only the ticker;
- QUOTES: copy one verbatim span of 8 to 30 words from the passage (for type D one from each passage) that supports the answer.
  It must be an exact character-for-character substring of the passage text (spacing aside). Prefer a distinctive span.
- skip=true (with skip_reason) if the passage is boilerplate, a table fragment, a forward-looking-statements disclaimer, too generic
  to identify, or unusable.`

phase('Draft')
const results = await pipeline(['A', 'B', 'C', 'D', 'E', 'F'],
  t => agent(`${BLIND}\nFILE: ${DIR}/type_${t}.json\n\n${COMMON}\n\n${RULES[t]}\n\nReturn every item (including skipped ones) via the schema.`,
    { label: `draft:${t}`, phase: 'Draft', schema: OUT, model: 'sonnet' }),
  (draft, t) => draft && agent(`${BLIND}\nFILE: ${DIR}/type_${t}.json\n\nYou are an independent checker of benchmark questions drafted from the passages in that file.
${COMMON}\n${RULES[t]}\n\nDRAFTS:\n${JSON.stringify(draft.items)}\n\nFor each drafted (non-skipped) item, verify against the passage text:
(1) every quote is an exact substring of its passage (ignoring whitespace differences); (2) the question is answerable from the
passage(s) and the answer is right; (3) the question names the company (both for D) and, for F, the period, for C the period;
(4) it is specific (not a question dozens of generic passages answer equally); (5) for A, C, E, F it does not copy the passage
sentence; (6) it fits its type. verdict: accept, fix (give the corrected question/quotes/answer), or reject (with reason). Skipped
drafts: return verdict reject with the skip reason. Return all refs via the schema.`,
    { label: `check:${t}`, phase: 'Check', schema: CHECKED, model: 'opus' }).then(c => ({ type: t, draft: draft.items, checked: c ? c.items : null })),
)
return results