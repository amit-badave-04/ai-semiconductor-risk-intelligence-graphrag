# semigraph web service — three stages:
#   1. model: quantize the public fp32 ONNX export of Qwen3-Embedding-0.6B to
#      8-bit weight-only (scripts/build_onnx_embedder.py; ~1.1 GB, no torch)
#   2. web: build the single-page app in web/ (Node 24.22.2, the lockfile, install scripts off); only the static
#      files in /web/dist leave this stage (docs/specs/m5b-frontend.md R13)
#   3. runtime: python:3.13-slim + the pinned serving deps + the semigraph wheel + the built app. No Node here.
# No secrets are baked in (see .dockerignore); config comes from Fly secrets.
# The python base image (both python stages) and the uv image are pinned by digest (the multi-arch index): a tag can move, a
# digest cannot. To move to a newer base, resolve the new index digest of the tag and change it here (one value for both python
# stages, tests/test_dockerfile_web.py); the serving requirements are hash-locked as well (deploy/requirements-serve.txt).
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
FROM python:3.13-slim@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc AS model
ENV PIP_NO_CACHE_DIR=1 HF_HUB_DISABLE_PROGRESS_BARS=1 HF_HOME=/hf
WORKDIR /build
# The model stage installs from hashed requirements, like the runtime does (R5-2): deploy/requirements-model.txt is generated
# with --generate-hashes from deploy/requirements-model.in, at the serving pins for every package the two locks share, and
# --require-hashes fails the build for a package that is unpinned or whose hash differs.
COPY deploy/requirements-model.txt deploy/
RUN pip install --require-hashes --only-binary=:all: -r deploy/requirements-model.txt
COPY scripts/build_onnx_embedder.py scripts/
RUN python scripts/build_onnx_embedder.py --out /models/qwen3-embedding-0.6b-q8 --no-verify \
 && python -c "import shutil; shutil.rmtree('/hf', ignore_errors=True)"
# The variant arguments come AFTER the 8-bit build, so changing them never invalidates that layer. fp32 pins the libraries
# to the serving pins (deploy/requirements-serve.txt) through the hashed deploy/requirements-model-fp32.txt: the build's bit-exactness
# check runs on the onnxruntime it serves with.
ARG EMBEDDER_VARIANT=q8
ARG KEEP_UNPATCHED=0
COPY deploy/requirements-model-fp32.txt deploy/
COPY scripts/predequantize_embedder.py scripts/embedder_timing.py scripts/
RUN set -eu; \
    case "$EMBEDDER_VARIANT" in \
      q8) ;; \
      fp32) pip install --require-hashes --only-binary=:all: -r deploy/requirements-model-fp32.txt; \
            python scripts/predequantize_embedder.py --src /models/qwen3-embedding-0.6b-q8 --out /models/qwen3-embedding-0.6b-fp32; \
            if [ "$KEEP_UNPATCHED" != 1 ]; then rm -rf /models/qwen3-embedding-0.6b-q8; fi ;; \
      *) echo "EMBEDDER_VARIANT must be q8 or fp32, got '$EMBEDDER_VARIANT'" >&2; exit 1 ;; \
    esac; \
    if [ "$KEEP_UNPATCHED" = 1 ]; then cp scripts/embedder_timing.py /models/embedder_timing.py; fi
# The memory probe comes in its own step AFTER the variant build, so editing it never re-runs the dequantization above.
COPY tools/probe/embed_rss.py scripts/
RUN if [ "$KEEP_UNPATCHED" = 1 ]; then cp scripts/embed_rss.py /models/embed_rss.py; fi

# Web stage. It sits BEFORE the runtime stage so the runtime stays the last (default) target. The tag is an exact version
# pinned by digest (the multi-arch index), the same Node as web/.node-version and the CI web job (tests/test_web_pins.py). The
# lockfile was created on Linux; `npm ci --ignore-scripts` installs exactly it. There is no build argument: one artifact,
# its base path fixed in web/vite.config.ts, serves /v2/ and (once UI_V2 flips the root) /.
FROM node:24.22.2-slim@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9 AS web
WORKDIR /web
COPY web/package.json web/package-lock.json web/.npmrc ./
RUN npm ci --ignore-scripts
COPY web/ ./
RUN npm run build
# The dist leaves this stage root-owned (the stage runs as root) and the runtime copies it WITHOUT --chown, so it stays root's
# (R13). Vite keeps the mode a web/public file has in the build context, and a context made under a restrictive umask (a local
# `fly deploy`) would give a root-owned 0600 file the app user cannot read: so the modes are set here, not left to the umask.
# Owner rwX; group and others read (and enter directories) and never write.
RUN chmod -R u=rwX,go=rX /web/dist

FROM python:3.13-slim@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc
ARG EMBEDDER_VARIANT=q8
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 EMBEDDING_BACKEND=onnx \
    ONNX_MODEL_PATH=/srv/models/qwen3-embedding-0.6b-${EMBEDDER_VARIANT}/model_${EMBEDDER_VARIANT}.onnx
COPY --from=ghcr.io/astral-sh/uv:0.11.20@sha256:eaa5f1a3305307aaf9e67fe2bbba1d85ebbb2d8a63bce23af21797bfafbe0f8b /uv /bin/uv
WORKDIR /srv
COPY deploy/requirements-serve.txt deploy/requirements-build.txt deploy/
# --require-hashes: every package of the file must carry a pinned hash that matches (deploy/requirements-serve.txt is generated
# with --generate-hashes), so an index that serves a different file fails the build.
RUN uv pip install --system --no-cache --require-hashes --no-build -r deploy/requirements-serve.txt
COPY pyproject.toml README.md ./
COPY src/ src/
# The project itself builds in an isolated environment; its build backend (hatchling and its dependencies) comes from the
# hash-locked deploy/requirements-build.txt as a build constraint, so uv checks every hash of it (R5-2).
RUN uv pip install --system --no-cache --no-deps --build-constraint deploy/requirements-build.txt .
# Never run as root; port 8080 needs no privileges. Nothing under /srv is handed to that user: /srv (the working directory of the
# server and of every upload worker it starts) and everything under it stay root-owned, so the serving user can write nothing on
# the import path or under /srv. The serving code writes nothing under /srv (state is in the database, temporary files go to
# /tmp). The serving user has no home directory (--no-create-home, /nonexistent), so there is none to write; caches go to /tmp
# (XDG_CACHE_HOME below), and PYTHONNOUSERSITE below keeps the user site off the import path as a second layer.
RUN useradd --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app
# The model layer is copied WITHOUT --chown (root-owned, like everything else here): models are root-owned and read-only to the
# serving user. The files keep the mode they had in the model stage, so the next step fails the BUILD (not the first boot) if app
# could not read one: the default model file must exist, and every model directory under /srv/models (a KEEP_UNPATCHED image
# holds the 8-bit one next to the fp32 one, and a staging app may point at either) must be world-readable, apart from the *.py
# probe scripts, which root runs. The same step refuses a symlink, any group- or world-writable entry and any owner but root
# anywhere under /srv/models. `find` runs inside an assignment, so a failing `find` stops the build; its output is tested after.
COPY --from=model /models /srv/models
RUN set -eu; \
    test -f "$ONNX_MODEL_PATH"; \
    bad="$(find /srv/models \( -type l -o -perm /022 -o ! -user root -o \( ! -name '*.py' \( \( -type f ! -perm -004 \) -o \( -type d ! -perm -005 \) \) \) \) -print -quit)"; \
    test -z "$bad" || { echo "unsafe model tree entry: $bad" >&2; exit 1; }
# The built app goes in AFTER the model layer, so a web change never invalidates the 1 GB model copy above. It is copied WITHOUT
# --chown on purpose (docs/specs/m5b-frontend.md R13): the files stay root-owned and world-readable, so the serving user reads the
# app it serves and cannot rewrite it. It is NOT under /srv, where it began: owner decision 5 (2026-11-07) put the dist at
# /opt/semigraph/web, under parents that are root's, and that stands now that /srv is root's as well. The COPY creates
# /opt/semigraph as root (the base image's /opt is root's) and nothing chowns or chmods /opt or anything under it, before or
# after this line (tests/test_dockerfile_web.py). Settings.ui_dist_dir defaults to this same path.
COPY --from=web /web/dist /opt/semigraph/web
# Image smoke check (deploy/checks/image_check.sh): root-owned and read-only (0555), copied before the privilege drop.
COPY --chmod=0555 deploy/checks/image_check.sh /srv/checks/image_check.sh
# PYTHONSAFEPATH=1 (Python 3.11+) keeps the working directory off sys.path for the server (`python -m ...`), and
# PYTHONNOUSERSITE=1 keeps the user site of /home/app off it, for the server and for anything that inherits them. The sandboxed
# upload workers get an allowlisted environment instead, so they start with -s -P on their own command line (uploads/sandbox.py
# module_command). Set after the build steps above. XDG_CACHE_HOME sends library caches to /tmp (the serving user has no home).
# Setuid and setgid bits come off every file of the image before the user switch, and the step fails if one is left: nothing the
# serving user starts gains privilege on exec (the workers also set no-new-privileges themselves).
RUN set -eu; \
    find / -xdev -perm /6000 -type f -exec chmod a-s {} +; \
    bad="$(find / -xdev -perm /6000 -type f -print -quit)"; \
    test -z "$bad"
ENV PYTHONSAFEPATH=1 PYTHONNOUSERSITE=1 XDG_CACHE_HOME=/tmp/.cache
USER app
# The build fails here if the serving user can write anything on the import path, under /srv or under /opt/semigraph, or any
# parent directory of those (it runs as that user, so `-w` and `find -writable` answer for it). Each path list is assigned first,
# so a failing `python` stops the step.
RUN set -eu; \
    paths="$(python -c 'import sys; print(*sys.path)')"; \
    for p in $paths /srv /opt/semigraph; do \
        [ -e "$p" ] || continue; \
        found="$(find "$p" -writable -print -quit)"; \
        [ -z "$found" ] || { echo "writable by the serving user: $found" >&2; exit 1; }; \
        while [ "$p" != / ]; do \
            if [ -w "$p" ]; then echo "writable by the serving user: $p" >&2; exit 1; fi; \
            p="$(dirname "$p")"; \
        done; \
    done
EXPOSE 8080
CMD ["python", "-m", "semigraph.serve.drain"]
