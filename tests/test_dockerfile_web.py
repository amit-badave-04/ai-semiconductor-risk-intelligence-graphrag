"""Dockerfile: the web build stage (docs/specs/m5b-frontend.md R13, S10; I1 part 3).

Docker is not available to these tests, so they pin the file itself: the stage order (the runtime image stays the LAST stage, so
it stays the default target), the digest-pinned Node base, the lockfile-only install with scripts off, the copy of the built app
into the runtime stage AFTER the 1 GB model layer (a web change must not invalidate it), and that no Node, npm or build output
leaks into the runtime stage. The model stage is covered by ``test_dockerfile_embedder.py`` and is not touched here.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
WEB_FROM = re.compile(r"^FROM node:24\.22\.2-slim@sha256:[0-9a-f]{64} AS web$")


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
    model_copy = "COPY --from=model --chown=app:app /models /srv/models"
    web_copy = "COPY --from=web --chown=app:app /web/dist /srv/web"
    assert model_copy in runtime and web_copy in runtime
    assert runtime.index(model_copy) < runtime.index(web_copy) < runtime.index("USER app")


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
