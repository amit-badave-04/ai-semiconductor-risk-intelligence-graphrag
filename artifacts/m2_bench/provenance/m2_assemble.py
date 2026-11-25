"""Assemble M2-Bench from both drafting rounds: merge Opus verdicts, bind, dedupe, tag type A on G (read-only), select, write."""
import glob
import importlib.util
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path("<repo>")
TASKS = Path("<tasks>")
SCR = TASKS.parent / "scratchpad"
OUTPUTS = [TASKS / "wg0jpi47o.output", TASKS / "wz05wfbog.output", TASKS / "wusriw37i.output"]
RETIRED_D = lambda ref: ref.startswith("D") and int(ref[1:]) < 64  # rounds 1-2 type D, retired after the spot-check
MAIN = {"dev": 5, "test": 10}
RESERVE = 5
spec = importlib.util.spec_from_file_location("m2_bench", ROOT / "scripts" / "m2_bench.py")
mb = importlib.util.module_from_spec(spec)
sys.modules["m2_bench"] = mb
spec.loader.exec_module(mb)
sys.path.insert(0, str(ROOT / "src"))
from semigraph.config import get_settings  # noqa: E402
from semigraph.graph import client  # noqa: E402
from semigraph.retrieval.retriever import detect_anchors  # noqa: E402

manifest = json.loads((ROOT / "artifacts/m2_bench/sample_manifest.json").read_text(encoding="utf-8"))
items = {it["ref"]: it for rnd in manifest["rounds"] for k in rnd["items"] for it in rnd["items"][k]}
lake = pd.concat([pd.read_parquet(p) for p in sorted(glob.glob(str(ROOT / "data/processed/chunks/*_chunks.parquet")))],
                 ignore_index=True)
by_cik = {cik: g for cik, g in lake.groupby("cik")}

cands, dropped = [], Counter()
for path in OUTPUTS:
    for block in json.loads(path.read_text(encoding="utf-8"))["result"]:
        t, drafts = block["type"], {d["ref"]: d for d in block["draft"]}
        for c in block["checked"] or []:
            if RETIRED_D(c["ref"]):
                dropped[(t, "retired_round_1_2")] += 1
                continue
            if c["verdict"] == "reject":
                dropped[(t, "rejected")] += 1
                continue
            d, fix = drafts.get(c["ref"], {}), c["verdict"] == "fix"
            q = (c.get("question") if fix and c.get("question") else d.get("question"))
            quotes = (c.get("quotes") if fix and c.get("quotes") else d.get("quotes"))
            answer = (c.get("answer") if fix and c.get("answer") else d.get("answer"))
            it = items[c["ref"]]
            if not q or not quotes or len(quotes) != len(it["chunk_ids"]):
                dropped[(t, "malformed")] += 1
                continue
            gold, ok = [], True
            for quote, cik, src in zip(quotes, it["ciks"], it["chunk_ids"]):
                try:
                    bound = mb.bind_quote(quote, by_cik[cik])
                except ValueError:
                    ok = False
                    break
                if src not in {g["chunk_id"] for g in bound} or not any(g["grade"] == 2 for g in bound):
                    ok = False
                    break
                gold += [{**g, "anchor": cik} for g in bound]
            if not ok:
                dropped[(t, "unbound")] += 1
                continue
            if not set(it["ciks"]) <= set(detect_anchors(q).values()):
                dropped[(t, "anchor_not_detected")] += 1
                continue
            cands.append({"id": f"M2-{c['ref']}", "type": t, "q": q, "answer": answer, "quotes": quotes,
                          "anchors": it["ciks"], "family": it["family"], "split": mb.split_of(it["family"]),
                          "gold": gold, "check": c["verdict"], "ref": c["ref"]})

# one question per text family (D first, then refs in order), and no two questions with the same current gold
cands.sort(key=lambda x: (x["type"] != "D", x["ref"]))
seen_fam, seen_gold, kept = set(), set(), []
for x in cands:
    fams = set(x["family"].split("+"))
    g2 = tuple(sorted(g["chunk_id"] for g in x["gold"] if g["grade"] == 2))
    if fams & seen_fam or g2 in seen_gold:
        dropped[(x["type"], "duplicate")] += 1
        continue
    seen_fam |= fams
    seen_gold.add(g2)
    kept.append(x)

# type A is machine-tagged: some grade-2 gold chunk must be filer-own (filer is an anchor, no MENTIONS edge to an anchor) on G
s = get_settings()
assert s.neo4j_uri == "bolt://localhost:7699", s.neo4j_uri
drv = client.get_driver(s)
Q = """UNWIND $ids AS cid MATCH (e:EvidenceSpan {chunk_id: cid}) OPTIONAL MATCH (e)-[:MENTIONS]->(c:Company)
RETURN e.chunk_id AS chunk_id, e.filer_cik AS filer, collect(c.cik) AS mentions"""
final = []
try:
    with drv.session(default_access_mode="READ") as ses:
        for x in kept:
            ids = [g["chunk_id"] for g in x["gold"]]
            rows = {r["chunk_id"]: r for r in ses.run(Q, ids=ids)}
            # gold counts only passages the graph holds (older 10-Qs inside the scope have no spans in the graph)
            x["gold"] = [g for g in x["gold"] if g["chunk_id"] in rows]
            x["gold_outside_graph"] = sorted(set(ids) - set(rows))
            per_anchor = {a: any(g["grade"] == 2 and g["anchor"] == a for g in x["gold"]) for a in x["anchors"]}
            if not all(per_anchor.values()):
                dropped[(x["type"], "no_current_gold_in_graph")] += 1
                continue
            anchors = set(x["anchors"])
            x["filer_own"] = any(g["grade"] == 2 and rows[g["chunk_id"]]["filer"] in anchors
                                 and not anchors & set(rows[g["chunk_id"]]["mentions"]) for g in x["gold"])
            if x["type"] == "A" and not x["filer_own"]:
                dropped[("A", "not_filer_own_in_graph")] += 1
                continue
            final.append(x)
finally:
    drv.close()

by = defaultdict(list)
for x in final:
    by[(x["type"], x["split"])].append(x)
out = {"dev": [], "test": []}
for t in mb.TYPES:
    for split in ("dev", "test"):
        pool = sorted(by[(t, split)], key=lambda x: x["ref"])
        for i, x in enumerate(pool[:MAIN[split] + RESERVE]):
            out[split].append({**{k: v for k, v in x.items() if k not in ("ref",)},
                               "role": "main" if i < MAIN[split] else "reserve", "reserve_order": max(0, i - MAIN[split] + 1)})
        print(t, split, "available", len(pool), "-> main", min(len(pool), MAIN[split]),
              "reserve", max(0, min(len(pool), MAIN[split] + RESERVE) - MAIN[split]))
print("dropped", dict(dropped))
for split, qs in out.items():
    doc = {"version": 1, "spec": "docs/specs/m2-search.md section 7", "split": split,
           "gold_policy": "every verbatim copy of each quote: grade 2 in the newest annual filing and later 10-Qs, grade 1 older",
           "drafting": "blind Claude drafters (filing passages only) + independent Opus check; rounds and seeds in sample_manifest.json",
           "questions": qs}
    (SCR / f"m2_bench_{split}.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
print("written to the scratchpad for review")
