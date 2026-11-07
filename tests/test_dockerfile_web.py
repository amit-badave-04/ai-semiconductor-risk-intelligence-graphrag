"""Dockerfile: the web build stage (docs/specs/m5b-frontend.md R13, S10; I1 part 3).

Docker is not available to these tests, so they pin the file itself: the stage order (the runtime image stays the LAST stage, so
it stays the default target), the digest-pinned Node base, the lockfile-only install with scripts off, the copy of the built app
into the runtime stage AFTER the 1 GB model layer (a web change must not invalidate it), and that no Node, npm or build output
leaks into the runtime stage. The working directory is root-owned (H1, owner-approved 2026-11-07): ``/srv`` and the models
under it stay root-owned (no ``chown`` of ``/srv``, no ``--chown`` on the model copy), so the serving user can write nothing on
the import path or under ``/srv``; ``PYTHONSAFEPATH=1`` keeps the working directory off ``sys.path`` and ``PYTHONNOUSERSITE=1``
keeps the user site of ``/home/app`` off it. The model stage is covered by ``test_dockerfile_embedder.py`` and is not touched
here.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
WEB_FROM = re.compile(r"^FROM node:24\.22\.2-slim@sha256:[0-9a-f]{64} AS web$")
MODEL_COPY = "COPY --from=model /models /srv/models"    # no --chown: models are root-owned and read-only to the serving user
WEB_COPY = "COPY --from=web --chown=app:app /web/dist /srv/web"
# The build fails here, not the first boot, when the serving user could not read what the root-owned copy brought in: the files
# keep the mode they had in the model stage (reasoned 0644 and 0755; nothing measured, no Docker on this desktop).
MODEL_READABLE = ('RUN test -f "$ONNX_MODEL_PATH" && test -z "$(find /srv/models ! -name \'*.py\' '
                  r'\( \( -type f ! -perm -004 \) -o \( -type d ! -perm -005 \) \) -print -quit)"')
WORKDIR_SRV = "WORKDIR /srv"
USERADD = "RUN useradd --create-home --shell /usr/sbin/nologin app"
SAFE_PATH = "PYTHONSAFEPATH=1"
NO_USER_SITE = "PYTHONNOUSERSITE=1"


def instructions(text: str) -> list[str]:
    """Dockerfile instructions with continuation lines joined and comments dropped."""
    out, current = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not current and (not line or line.startswith("#")):
            continue
        if current and line.startswith("#"):
            continue
        current += (" " if current else "") + line.removesuffix("\\").strip()
        if not line.endswith("\\"):
            out.append(current)
            current = ""
    return out


def stages() -> dict[str, list[str]]:
    """Stage name (``model``, ``web``, ``runtime``) to its instructions, ``FROM`` line first."""
    steps = instructions(DOCKERFILE.read_text(encoding="utf-8"))
    starts = [i for i, step in enumerate(steps) if step.startswith("FROM ")]
    bounds = [*starts, len(steps)]
    out = {}
    for n, start in enumerate(starts):
        block = steps[start:bounds[n + 1]]
        name = block[0].rsplit(" AS ", 1)[1] if " AS " in block[0] else "runtime"
        out[name] = block
    return out


STAGES = stages()


def test_the_stages_are_model_then_web_then_the_runtime_image_last():
    assert list(STAGES) == ["model", "web", "runtime"]
    assert STAGES["runtime"][0] == "FROM python:3.13-slim"          # the last stage: the default build target, unchanged


def test_the_web_stage_is_a_digest_pinned_exact_node_tag():
    assert WEB_FROM.match(STAGES["web"][0]), STAGES["web"][0]


def test_the_web_stage_installs_from_the_lockfile_with_scripts_off_and_builds_once():
    """The .npmrc (scripts off, engine-strict) is copied with the lockfile, before the install; the install states
    ``--ignore-scripts`` on its own command line as well; the sources come after the install so an edit keeps its layer."""
    web = STAGES["web"]
    assert "WORKDIR /web" in web
    copy_lock = web.index("COPY web/package.json web/package-lock.json web/.npmrc ./")
    install = web.index("RUN npm ci --ignore-scripts")
    copy_all = web.index("COPY web/ ./")
    build = web.index("RUN npm run build")
    assert copy_lock < install < copy_all < build
    text = "\n".join(web)
    assert not re.search(r"npm (install|i|add|update)\b", text)
    assert "--base" not in text and "ARG" not in text, "one artifact: the base path is fixed in vite.config.ts (R12)"


def test_the_runtime_stage_gets_the_built_app_after_the_model_layer():
    runtime = STAGES["runtime"]
    assert MODEL_COPY in runtime and WEB_COPY in runtime
    assert runtime.index(MODEL_COPY) < runtime.index(WEB_COPY) < runtime.index("USER app")


# ---------------------------------------------------------------- the working directory and the models (H1, 2026-11-07)

def test_nothing_under_srv_is_handed_to_the_serving_user():
    """``/srv`` is the working directory of the server and of every upload worker. The user is created without a ``chown`` of it
    and the model layer is copied without ``--chown``, so the serving user can write nothing on the import path or under ``/srv``
    but the built app, which the web copy hands over at this step (it moves in the next commit). The only ``chown`` left in the
    runtime stage is that one ``--chown`` on the web copy. Docker is not available to these tests: this pins the file, so the
    ownership itself is not measured in an image."""
    runtime = STAGES["runtime"]
    assert USERADD in runtime
    assert [step for step in runtime if "chown" in step] == [WEB_COPY]
    assert MODEL_COPY in runtime and "--chown" not in MODEL_COPY


def test_the_server_starts_in_srv_which_the_image_creates_as_root():
    """The working directory of ``CMD`` (and so of every child the server starts: none sets ``cwd=``) is the last ``WORKDIR``
    before the user switch. ``WORKDIR`` makes the directory as root, and no step hands it on (the test above)."""
    runtime = STAGES["runtime"]
    before_user = runtime[: runtime.index("USER app")]
    assert [step for step in before_user if step.startswith("WORKDIR ")][-1] == WORKDIR_SRV
    assert runtime[-1].startswith("CMD ") and runtime.index("USER app") < len(runtime) - 1


def test_the_runtime_stage_takes_the_working_directory_and_the_user_site_off_the_import_path():
    """``PYTHONSAFEPATH=1`` (Python 3.11+) for the server (``python -m ...`` puts the working directory first on ``sys.path``)
    and ``PYTHONNOUSERSITE=1`` for the one directory the serving user owns (``/home/app``). The sandboxed workers do not inherit
    them (their environment is an allowlist), so they carry ``-P`` and ``-s`` on their own argv
    (``tests/test_sandbox_safe_path.py``). Both are set before the user switch, in an ``ENV`` of their own after the first one,
    which the embedder test reads for ``ONNX_MODEL_PATH``."""
    runtime = STAGES["runtime"]
    envs = [step for step in runtime if step.startswith("ENV ")]
    for setting in (SAFE_PATH, NO_USER_SITE):
        assert any(setting in step.split() for step in envs), (setting, envs)
        assert runtime.index(next(step for step in envs if setting in step.split())) < runtime.index("USER app"), setting
        assert not any(setting in step for name in ("model", "web") for step in STAGES[name])   # those stages run no Python for the app


def test_the_model_files_are_checked_readable_by_the_serving_user_at_build_time():
    """Without ``--chown`` the model files keep the owner and mode they had in the model stage. A mode that left them
    unreadable to ``app`` would otherwise show only at the first boot; this step fails the image build instead. It covers every
    model directory under ``/srv/models`` (a ``KEEP_UNPATCHED`` image holds the 8-bit model beside the fp32 one, and a staging
    app may serve either), not only the default model's, and skips the ``*.py`` probe scripts, which root runs. It sits right
    after the model copy and before the user switch, so it runs as root against exactly the files that were copied."""
    runtime = STAGES["runtime"]
    assert MODEL_READABLE in runtime
    assert runtime.index(MODEL_COPY) < runtime.index(MODEL_READABLE) < runtime.index("USER app")


def test_the_dist_lands_where_the_settings_default_looks_for_it():
    from semigraph.config import Settings

    assert Settings.model_fields["ui_dist_dir"].default == Path("/srv/web")


def test_the_runtime_stage_has_no_node_npm_or_web_sources():
    runtime = STAGES["runtime"]
    text = "\n".join(runtime).lower()
    for forbidden in ("node", "npm", "npx", "package.json"):
        assert forbidden not in text, forbidden
    assert re.findall(r"--from=web (?:--chown=\S+ )?(\S+)", "\n".join(runtime)) == ["/web/dist"]


def test_the_model_stage_and_the_runtime_variant_arguments_are_unchanged():
    assert "ARG EMBEDDER_VARIANT=q8" in STAGES["model"] and "ARG KEEP_UNPATCHED=0" in STAGES["model"]
    assert "ARG EMBEDDER_VARIANT=q8" in STAGES["runtime"]
    assert STAGES["runtime"][-1].startswith("CMD ")


def ignored_lines() -> list[str]:
    return [ln.strip() for ln in DOCKERIGNORE.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]


def test_dockerignore_keeps_node_modules_and_build_output_out_of_the_context():
    lines = ignored_lines()
    assert "web/node_modules" in lines and "web/dist" in lines
    # the root `dist/` entry matches only the root (dockerignore matching rules), so web/dist needs its own line.
    assert "dist/" in lines


def test_dockerignore_does_not_exclude_what_the_web_stage_copies():
    lines = ignored_lines()
    needed = ("web/package.json", "web/package-lock.json", "web/.npmrc", "web/index.html", "web/src/main.tsx",
              "web/src/lib/ids.generated.ts", "web/vite.config.ts", "web/tsconfig.json")
    for path in needed:
        for pattern in lines:
            stripped = pattern.rstrip("/")
            assert not (path == stripped or path.startswith(stripped + "/")), (path, pattern)


def test_the_secrets_rules_of_dockerignore_stay():
    lines = ignored_lines()
    assert ".env" in lines and ".env.*" in lines
    assert "!.env.example" in [ln.strip() for ln in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()]
