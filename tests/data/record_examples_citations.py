"""Record ``{example id: CITE_RE.findall(answer)}`` for every shipped example answer (tests/test_ids.py pins it).

Written once from the pre-M4 citation grammar (commit cff2415) so that extending the grammar with ``doc:`` ids provably changes
nothing any shipped answer cites. Re-record only for a deliberate grammar change or a regenerated examples.json:
    PYTHONPATH=src .venv/Scripts/python tests/data/record_examples_citations.py
"""

import json
from pathlib import Path

from semigraph.artifacts import load_examples
from semigraph.retrieval.ids import CITE_RE, classify_id

OUT = Path(__file__).parent / "examples_citations_pre_m4.json"

if __name__ == "__main__":
    examples = load_examples()["examples"]
    record = {e["id"]: {"citations": CITE_RE.findall(e["answer"]),
                        "kinds": [classify_id(c) for c in CITE_RE.findall(e["answer"])]} for e in examples}
    OUT.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"recorded {len(record)} examples -> {OUT}")
