"""Evidence for the embedder-gate decision (NOT a replacement for the pre-registered gate): how close is each of the
shipped 8-bit model (A) and the accuracy_level=4 model (B) to the fp32 sentence-transformers model that the corpus
vectors were built from? Production prompt, the 60-question default set, brute-force cosine over the real corpus.
Run from the repo root with the dev env, offline. argv: A_path B_path out.json"""
import glob
import json
import os
import sys

os.environ["HF_HUB_OFFLINE"] = "1"
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

sys.path.insert(0, "src")
from semigraph.embeddings_onnx import OnnxBackend  # noqa: E402

A_PATH, B_PATH, OUT = sys.argv[1], sys.argv[2], sys.argv[3]


def collect(obj, out):
    if isinstance(obj, dict):
        for key in ("q", "question"):
            v = obj.get(key)
            if isinstance(v, str) and 10 < len(v) < 600:
                out.append(" ".join(v.split()))
        for v in obj.values():
            collect(v, out)
    elif isinstance(obj, list):
        for v in obj:
            collect(v, out)


questions: list[str] = []
for p in ("src/semigraph/artifacts/benchmark.json", "src/semigraph/artifacts/examples.json"):
    collect(json.load(open(p, encoding="utf-8")), questions)
questions = list(dict.fromkeys(questions))

corpus = np.vstack([np.asarray(e, dtype=np.float64) for df in
                    (pd.read_parquet(f) for f in sorted(glob.glob("data/processed/embeddings/*.parquet")))
                    for e in df["embedding"]])
corpus /= np.linalg.norm(corpus, axis=1, keepdims=True)

from sentence_transformers import SentenceTransformer  # noqa: E402

st = SentenceTransformer("Qwen/Qwen3-Embedding-0.6B", device="cpu")
fp32 = np.asarray(st.encode(questions, prompt_name="query", normalize_embeddings=True, batch_size=8), dtype=np.float64)


def onnx_vectors(path: str) -> np.ndarray:
    backend = OnnxBackend(path)
    return np.asarray([backend.encode_query(q) for q in questions], dtype=np.float64)


vecs = {"shipped_q8": onnx_vectors(A_PATH), "patched_acc4": onnx_vectors(B_PATH)}
s_ref = fp32 @ corpus.T
ref_top = np.argsort(-s_ref, axis=1, kind="stable")
report = {"questions": len(questions), "corpus_vectors": int(corpus.shape[0]), "prompt": "sentence-transformers 'query' prompt = production QUERY_PROMPT",
          "models": {}}
top1_match = {}
for name, v in vecs.items():
    s = v @ corpus.T
    top = np.argsort(-s, axis=1, kind="stable")
    cos = np.sum(v * fp32, axis=1) / (np.linalg.norm(v, axis=1) * np.linalg.norm(fp32, axis=1))
    t1 = top[:, 0] == ref_top[:, 0]
    top1_match[name] = t1
    ov8 = [len(set(top[i, :8]) & set(ref_top[i, :8])) / 8 for i in range(len(questions))]
    regret = [float(s_ref[i, ref_top[i, 0]] - s_ref[i, top[i, 0]]) for i in range(len(questions))]
    report["models"][name] = {
        "cosine_to_fp32": {"mean": round(float(cos.mean()), 5), "min": round(float(cos.min()), 5)},
        "top1_agreement_with_fp32": round(float(t1.mean()), 4), "top1_disagreements": int((~t1).sum()),
        "mean_top8_overlap_with_fp32": round(float(np.mean(ov8)), 4),
        "mean_regret_under_fp32_score": round(float(np.mean(regret)), 6), "max_regret": round(float(np.max(regret)), 6),
    }
a, b = top1_match["shipped_q8"], top1_match["patched_acc4"]
report["paired_top1"] = {"both_agree_with_fp32": int((a & b).sum()), "only_patched_agrees": int((~a & b).sum()),
                         "only_shipped_agrees": int((a & ~b).sum()), "neither_agrees": int((~a & ~b).sum())}
json.dump(report, open(OUT, "w"), indent=1)
print(json.dumps(report, indent=1))
