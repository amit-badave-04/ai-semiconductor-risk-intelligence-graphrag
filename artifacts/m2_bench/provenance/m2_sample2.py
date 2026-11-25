"""Round 2 of the M2-Bench sample: the thin types, excluding every round-1 family (drafting input written to the scratchpad)."""
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

SEED, COUNTS = 20261127, {"A": 16, "B": 40, "C": 20, "D": 0, "E": 12, "F": 16}
lake = pd.concat([pd.read_parquet(p) for p in sorted(glob.glob(str(ROOT / "data/processed/chunks/*_chunks.parquet")))],
                 ignore_index=True)
ents = json.loads((ROOT / "src/semigraph/artifacts/canonical_entities.json").read_text(encoding="utf-8"))
aliases = {int(e["cik"]): [n, *(e.get("aliases") or []), *([e["ticker"]] if e.get("ticker") else [])]
           for n, e in ents.items() if e.get("cik")}
mpath = ROOT / "artifacts" / "m2_bench" / "sample_manifest.json"
manifest = json.loads(mpath.read_text(encoding="utf-8"))
round1 = manifest["items"] if "rounds" not in manifest else manifest["rounds"][0]["items"]
drawn = {f for items in round1.values() for it in items for f in it["family"].split("+")}
sample = mb.sample_passages(lake, seed=SEED, per_type=COUNTS, aliases=aliases, exclude_families=drawn)
print({k: len(v) for k, v in sample.items()})
items2 = {k: [{"ref": f"{k}{32 + i:02d}", **it} for i, it in enumerate(v)] for k, v in sample.items() if v}
manifest = {"generator": "scripts/m2_bench.py sample_passages",
            "rounds": [{"seed": manifest.get("seed", 20261126), "per_type": manifest.get("per_type", 32), "items": round1},
                       {"seed": SEED, "per_type": COUNTS, "exclude": "every round-1 family", "items": items2}]}
mpath.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8", newline="\n")

text = dict(zip(lake["chunk_id"], lake["text"]))
meta = lake.set_index("chunk_id")[["ticker", "form", "filing_date", "section_id"]].to_dict("index")
out = SCR / "m2_blind2"
out.mkdir(exist_ok=True)
for k, v in items2.items():
    data = [{"ref": it["ref"], "passages": [{"company": meta[c]["ticker"], "form": meta[c]["form"],
                                             "filing_date": meta[c]["filing_date"], "section": meta[c]["section_id"],
                                             "text": text[c]} for c in it["chunk_ids"]]} for it in v]
    (out / f"type_{k}.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
print("ok", sorted(p.name for p in out.iterdir()))
