"""Fetch the MiniLM cross-encoder reranker files for the image (M2 spec R24, gate R13). Stdlib only: no torch, no pip.

Downloads three files of cross-encoder/ms-marco-MiniLM-L6-v2 from huggingface.co, checks every one against a pinned SHA-256 and
refuses to write anything on a mismatch, so the build fails instead of shipping an unverified model. The layout is the one
src/semigraph/retrieval/rerank.py expects under RETRIEVAL_RERANK_MODEL_DIR: <out>/onnx/model_quint8_avx2.onnx, <out>/tokenizer.json
and <out>/config.json.

    python scripts/build_reranker_model.py --out /models/reranker-minilm-l6
"""

import argparse
import hashlib
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

REPO = "cross-encoder/ms-marco-MiniLM-L6-v2"
# The repo's commit sha (Hugging Face model API, 2026-11-26: lastModified 2026-08-09, the revision the local files were downloaded
# and hashed from). The SHA-256 below stays the integrity guarantee: anything that does not serve these exact bytes fails the build.
REVISION = "233902d25c440f23af6f7d6e94d2946bac0bee0a"
# path inside the repo -> (size in bytes, full SHA-256). The ONNX hash is of models/reranker-minilm-l6/onnx/model_quint8_avx2.onnx
# (the file the M2 bench was run with; prefix c80a8b34256ea453).
FILES = {
    "onnx/model_quint8_avx2.onnx": (23_200_716, "c80a8b34256ea453093d612e3ac48d3d965a0c0a48c7906709af8b8e28461bf9"),
    "tokenizer.json": (None, "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"),
    "config.json": (None, "380e02c93f431831be65d99a4e7e5f67c133985bf2e77d9d4eba46847190bacc"),
}
TIMEOUT_S = 120


def _allowed(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    return parts.scheme == "https" and (host in ("huggingface.co", "hf.co") or host.endswith(".hf.co"))


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects (resolve/ goes to an HF CDN) only to https huggingface.co / *.hf.co hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed(newurl):
            raise ValueError(f"refusing a redirect to {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http_get(url: str) -> bytes:
    if not url.startswith("https://huggingface.co/"):
        raise ValueError(f"refusing a non-huggingface url: {url}")
    opener = urllib.request.build_opener(_SafeRedirect)
    with opener.open(url, timeout=TIMEOUT_S) as response:  # noqa: S310 - host checked above, redirects by _SafeRedirect
        return response.read()


def verify(name: str, data: bytes) -> None:
    size, digest = FILES[name]
    if size is not None and len(data) != size:
        raise ValueError(f"{name}: {len(data)} bytes, expected {size}")
    actual = hashlib.sha256(data).hexdigest()
    if actual != digest:
        raise ValueError(f"{name}: SHA-256 {actual}, expected {digest}")


def build(out: Path, fetch: Callable[[str], bytes] | None = None) -> None:
    fetch = fetch or _http_get          # looked up at call time, so a test can replace _http_get
    fetched = {}
    for name in FILES:
        data = fetch(f"https://huggingface.co/{REPO}/resolve/{REVISION}/{name}")
        verify(name, data)
        fetched[name] = data
    for name, data in fetched.items():       # nothing is written unless all three verified
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        build(args.out)
    except Exception as exc:  # noqa: BLE001 - any failure must fail the image build with a message
        print(f"reranker model build failed: {exc}", file=sys.stderr)
        return 1
    print(f"reranker model written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
