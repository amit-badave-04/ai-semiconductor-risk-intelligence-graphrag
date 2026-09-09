# semigraph web service — two stages:
#   1. model: quantize the public fp32 ONNX export of Qwen3-Embedding-0.6B to
#      8-bit weight-only (scripts/build_onnx_embedder.py; ~1.1 GB, no torch)
#   2. runtime: python:3.13-slim + the pinned serving deps + the semigraph wheel
# No secrets are baked in (see .dockerignore); config comes from Fly secrets.
FROM python:3.13-slim AS model
ENV PIP_NO_CACHE_DIR=1 HF_HUB_DISABLE_PROGRESS_BARS=1 HF_HOME=/hf
RUN pip install "onnx>=1.17" "onnxruntime>=1.29" "onnx-ir>=1.0" "huggingface_hub>=1.0" "numpy>=2"
WORKDIR /build
COPY scripts/build_onnx_embedder.py scripts/
RUN python scripts/build_onnx_embedder.py --out /models/qwen3-embedding-0.6b-q8 --no-verify \
 && python -c "import shutil; shutil.rmtree('/hf', ignore_errors=True)"

FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 EMBEDDING_BACKEND=onnx \
    ONNX_MODEL_PATH=/srv/models/qwen3-embedding-0.6b-q8/model_q8.onnx
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
CMD ["uvicorn", "semigraph.serve.main:app", "--host", "0.0.0.0", "--port", "8080"]
