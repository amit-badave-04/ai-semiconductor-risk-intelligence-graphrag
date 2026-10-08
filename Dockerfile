# semigraph web service — two stages:
#   1. model: quantize the public fp32 ONNX export of Qwen3-Embedding-0.6B to
#      8-bit weight-only (scripts/build_onnx_embedder.py; ~1.1 GB, no torch)
#   2. runtime: python:3.13-slim + the pinned serving deps + the semigraph wheel
# No secrets are baked in (see .dockerignore); config comes from Fly secrets.
#
# Embedder variant (--build-arg EMBEDDER_VARIANT=q8|fp32, default q8 = the image that is live):
#   q8    the 8-bit model above, untouched; ONNX Runtime dequantizes every weight on every call.
#   fp32  the same weights dequantized ONCE offline (scripts/predequantize_embedder.py): a 2.3 GB
#         model at /srv/models/qwen3-embedding-0.6b-fp32/model_fp32.onnx (+ .data), same retrieval,
#         about 4x faster per query, about 640 MB more memory (measured on Windows; Linux/Fly not
#         measured yet). The 8-bit model is dropped from the image unless KEEP_UNPATCHED=1.
#   --build-arg KEEP_UNPATCHED=1 keeps the 8-bit model next to the fp32 one and ships
#         scripts/embedder_timing.py as /srv/models/embedder_timing.py, for the Fly timing window:
#         python /srv/models/embedder_timing.py /srv/models/qwen3-embedding-0.6b-q8/model_q8.onnx \
#                /srv/models/qwen3-embedding-0.6b-fp32/model_fp32.onnx -n 60 --threads 1
#         and tools/probe/embed_rss.py as /srv/models/embed_rss.py, the resident-memory probe (one model per process):
#         python /srv/models/embed_rss.py /srv/models/qwen3-embedding-0.6b-q8/model_q8.onnx --out q8.json
#         python /srv/models/embed_rss.py /srv/models/qwen3-embedding-0.6b-fp32/model_fp32.onnx --compare q8.json
# The image ENV ONNX_MODEL_PATH follows the variant, but fly.toml's [env] ONNX_MODEL_PATH overrides it:
# serving an fp32 image means changing that one line (a decision for after the timing/RSS window).
# An fp32 image built WITHOUT KEEP_UNPATCHED has no 8-bit file, so with fly.toml unchanged it fails to boot.
FROM python:3.13-slim AS model
ENV PIP_NO_CACHE_DIR=1 HF_HUB_DISABLE_PROGRESS_BARS=1 HF_HOME=/hf
RUN pip install "onnx>=1.17" "onnxruntime>=1.29" "onnx-ir>=1.0" "huggingface_hub>=1.0" "numpy>=2"
WORKDIR /build
COPY scripts/build_onnx_embedder.py scripts/
RUN python scripts/build_onnx_embedder.py --out /models/qwen3-embedding-0.6b-q8 --no-verify \
 && python -c "import shutil; shutil.rmtree('/hf', ignore_errors=True)"
# The variant arguments come AFTER the 8-bit build, so changing them never invalidates that layer. fp32 pins the libraries
# to the serving pins (deploy/requirements-serve.txt): the build's bit-exactness check runs on the onnxruntime it serves with.
ARG EMBEDDER_VARIANT=q8
ARG KEEP_UNPATCHED=0
COPY scripts/predequantize_embedder.py scripts/embedder_timing.py scripts/
RUN set -eu; \
    case "$EMBEDDER_VARIANT" in \
      q8) ;; \
      fp32) pip install "onnx==1.22.0" "onnxruntime==1.29.0" "numpy==2.5.3" "tokenizers==0.23.2"; \
            python scripts/predequantize_embedder.py --src /models/qwen3-embedding-0.6b-q8 --out /models/qwen3-embedding-0.6b-fp32; \
            if [ "$KEEP_UNPATCHED" != 1 ]; then rm -rf /models/qwen3-embedding-0.6b-q8; fi ;; \
      *) echo "EMBEDDER_VARIANT must be q8 or fp32, got '$EMBEDDER_VARIANT'" >&2; exit 1 ;; \
    esac; \
    if [ "$KEEP_UNPATCHED" = 1 ]; then cp scripts/embedder_timing.py /models/embedder_timing.py; fi
# The memory probe comes in its own step AFTER the variant build, so editing it never re-runs the dequantization above.
COPY tools/probe/embed_rss.py scripts/
RUN if [ "$KEEP_UNPATCHED" = 1 ]; then cp scripts/embed_rss.py /models/embed_rss.py; fi

FROM python:3.13-slim
ARG EMBEDDER_VARIANT=q8
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 EMBEDDING_BACKEND=onnx \
    ONNX_MODEL_PATH=/srv/models/qwen3-embedding-0.6b-${EMBEDDER_VARIANT}/model_${EMBEDDER_VARIANT}.onnx
COPY --from=ghcr.io/astral-sh/uv:0.11.20 /uv /bin/uv
WORKDIR /srv
COPY deploy/requirements-serve.txt deploy/
RUN uv pip install --system --no-cache -r deploy/requirements-serve.txt
COPY pyproject.toml README.md ./
COPY src/ src/
RUN uv pip install --system --no-cache --no-deps .
# Never run as root; port 8080 needs no privileges. The 1 GB model layer is
# copied with its final owner so no chown re-writes it as a second layer.
RUN useradd --create-home --shell /usr/sbin/nologin app && chown -R app:app /srv
COPY --from=model --chown=app:app /models /srv/models
USER app
EXPOSE 8080
CMD ["python", "-m", "semigraph.serve.drain"]
