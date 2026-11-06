// Test helper of tests/test_export_ids.py (docs/specs/m5b-frontend.md R5): runs the GENERATED id patterns under V8.
//
//   node tests/data/citation_corpus_eval.mjs <ids.generated.ts> <pages.generated.ts> <citation_corpus.json>
//
// It imports the two generated TypeScript files as they are (Node strips the types), classifies every id of the corpus and
// scans every text of it with nothing but those constants, and prints one JSON object: the results in corpus order, how many
// entries it processed, whether each pattern string compiles with no flag, with `u` and with `v`, the flags of the exported
// RegExp objects, and the pages file's values. The test compares them with the hand-written labels of the corpus; this file
// holds no expectation. It is NOT the shipped tokenizer or classifier (those arrive with the Ask page); it is the smallest
// reader of the generated constants, so a pattern the translation broke fails here.
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [idsPath, pagesPath, corpusPath] = process.argv.slice(2);
if (!idsPath || !pagesPath || !corpusPath) {
  console.error("usage: node citation_corpus_eval.mjs <ids.generated.ts> <pages.generated.ts> <citation_corpus.json>");
  process.exit(2);
}

const ids = await import(pathToFileURL(idsPath).href);
const pages = await import(pathToFileURL(pagesPath).href);
const corpus = JSON.parse(readFileSync(corpusPath, "utf8"));

const FORMS = [["chunk", ids.CHUNK_ID_RE], ["xbrl", ids.XBRL_ID_RE], ["fr", ids.FR_ID_RE], ["doc", ids.DOC_ID_RE]];
const classify = (id) => {
  for (const [kind, re] of FORMS) if (re.test(id)) return kind;
  return null;
};
const scan = (text) => Array.from(text.matchAll(new RegExp(ids.CITE_PATTERN, "g")), (m) => m[1]);

const processed = { classify: 0, text: 0 };
const classified = corpus.classify.map((entry) => {
  processed.classify += 1;
  return classify(entry.id);
});
const scanned = corpus.text.map((entry) => {
  processed.text += 1;
  return scan(entry.text);
});

const strings = Object.keys(ids).filter((name) => typeof ids[name] === "string");
const compile = {};
for (const flags of ["", "u", "v"]) {
  compile[flags || "none"] = Object.fromEntries(
    strings.map((name) => {
      try {
        new RegExp(ids[name], flags);
        return [name, true];
      } catch (error) {
        return [name, String(error.message)];
      }
    }),
  );
}

const regexpFlags = Object.fromEntries(FORMS.map(([, re], n) => [["CHUNK_ID_RE", "XBRL_ID_RE", "FR_ID_RE", "DOC_ID_RE"][n], re.flags]));

process.stdout.write(
  JSON.stringify({
    classify: classified,
    text: scanned,
    processed,
    strings,
    compile,
    regexpFlags,
    pages: { SPA_PAGES: [...pages.SPA_PAGES], FILERS: pages.FILERS.map((f) => ({ ticker: f.ticker, name: f.name })) },
  }),
);
