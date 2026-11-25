"""Round 3 of the M2-Bench sample: type D redrawn as topic-matched pairs (owner spot-check, 2026-11-26)."""
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

SEED, N = 20261128, 36
TOPICS = ("export control", "section 232", "ai diffusion", "entity list", "tsmc", "hbm", "rare earth", "tariff",
          "openai", "advanced packaging", "cybersecurity incident", "chips act", "taiwan", "share repurchase",
          "gross margin", "data center")
lake = pd.concat([pd.read_parquet(p) for p in sorted(glob.glob(str(ROOT / "data/processed/chunks/*_chunks.parquet")))],
                 ignore_index=True)
mpath = ROOT / "artifacts" / "m2_bench" / "sample_manifest.json"
manifest = json.loads(mpath.read_text(encoding="utf-8"))
drawn = {f for rnd in manifest["rounds"] for items in rnd["items"].values() for it in items for f in it["family"].split("+")}
pairs = mb.sample_topic_pairs(lake, seed=SEED, n=N, topics=TOPICS, exclude_families=drawn)
print(len(pairs), "pairs; topics:", sorted({p["topic"] for p in pairs}))
items3 = {"D": [{"ref": f"D{64 + i:02d}", **p} for i, p in enumerate(pairs)]}
manifest["rounds"].append({"seed": SEED, "per_type": {"D": N}, "topics": list(TOPICS),
                           "note": "type D redrawn as topic-matched pairs after the owner's spot-check; rounds 1-2 type D retired",
                           "exclude": "every round-1 and round-2 family", "items": items3})
mpath.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8", newline="\n")
text = dict(zip(lake["chunk_id"], lake["text"]))
meta = lake.set_index("chunk_id")[["ticker", "form", "filing_date", "section_id"]].to_dict("index")
out = SCR / "m2_blind3"
out.mkdir(exist_ok=True)
data = [{"ref": it["ref"], "shared_topic": it["topic"],
         "passages": [{"company": meta[c]["ticker"], "form": meta[c]["form"], "filing_date": meta[c]["filing_date"],
                       "section": meta[c]["section_id"], "text": text[c]} for c in it["chunk_ids"]]} for it in items3["D"]]
(out / "type_D.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
print("ok")
