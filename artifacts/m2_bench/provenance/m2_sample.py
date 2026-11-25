import glob
import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path("<repo>")
SCR = Path("<scratch>")
spec = importlib.util.spec_from_file_location("m2_bench", ROOT / "scripts" / "m2_bench.py")
mb = importlib.util.module_from_spec(spec)
sys.modules["m2_bench"] = mb
spec.loader.exec_module(mb)

SEED, PER_TYPE = 20261126, 32
lake = pd.concat([pd.read_parquet(p) for p in sorted(glob.glob(str(ROOT / "data/processed/chunks/*_chunks.parquet")))],
                 ignore_index=True)
ents = json.loads((ROOT / "src/semigraph/artifacts/canonical_entities.json").read_text(encoding="utf-8"))
aliases = {}
for name, e in ents.items():
    if e.get("cik"):
        aliases[int(e["cik"])] = [name, *(e.get("aliases") or []), *([e["ticker"]] if e.get("ticker") else [])]
sample = mb.sample_passages(lake, seed=SEED, per_type=PER_TYPE, aliases=aliases)
print({k: len(v) for k, v in sample.items()}, "in-scope filings:", len(mb.in_scope_accessions(lake)))

text = dict(zip(lake["chunk_id"], lake["text"]))
meta = lake.set_index("chunk_id")[["ticker", "form", "filing_date", "section_id"]].to_dict("index")
manifest = {"seed": SEED, "per_type": PER_TYPE, "generator": "scripts/m2_bench.py sample_passages",
            "items": {k: [{"ref": f"{k}{i:02d}", **it} for i, it in enumerate(v)] for k, v in sample.items()}}
out_dir = ROOT / "artifacts" / "m2_bench"
out_dir.mkdir(parents=True, exist_ok=True)
(out_dir / "sample_manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8", newline="\n")
drafting = {k: [{"ref": it["ref"], "passages": [{"company": meta[c]["ticker"], "form": meta[c]["form"],
                                                   "filing_date": meta[c]["filing_date"], "section": meta[c]["section_id"],
                                                   "text": text[c]} for c in it["chunk_ids"]]}
                for it in manifest["items"][k]] for k in manifest["items"]}
(SCR / "m2_drafting_input.json").write_text(json.dumps(drafting, indent=1), encoding="utf-8")
print("ok")
