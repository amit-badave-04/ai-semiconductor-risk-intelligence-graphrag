"""Dockerfile: the web build stage (docs/specs/m5b-frontend.md R13, S10; I1 part 3).

Docker is not available to these tests, so they pin the file itself: the stage order (the runtime image stays the LAST stage, so
it stays the default target), the digest-pinned Node base, the lockfile-only install with scripts off, the copy of the built app
into the runtime stage AFTER the 1 GB model layer (a web change must not invalidate it), and that no Node, npm or build output
leaks into the runtime stage. The dist stays root-owned (no ``--chown``) and world-readable (an explicit mode step in the web
stage), so the serving user can read the app and cannot rewrite it (R13 amendment, owner decision 2026-11-07). It also sits at
``/opt/semigraph/web``, not under ``/srv`` (owner decision 5, 2026-11-07), and ``/opt`` and ``/opt/semigraph`` stay root-owned
(no step chowns or chmods them). ``/srv`` is root-owned as well (H1, owner-approved 2026-11-07): ``/srv`` and the models under it
stay root-owned (no ``chown`` of ``/srv``, no ``--chown`` on the model copy), because the server and every upload worker start
in ``/srv``, so the serving user can write nothing on the import path or under ``/srv``. ``PYTHONSAFEPATH=1`` keeps the working
directory off ``sys.path`` and ``PYTHONNOUSERSITE=1`` keeps the user site off it (I1 security review, 2026-11-07). No
instruction (``RUN``, ``COPY`` or ``ADD``) may use a heredoc: the scans below do not read its body (``heredoc_steps``).
Image hardening (commit 8, owner-approved 2026-11-07; the I4 image build is the proof, nothing here builds an image): the
Python base image and the ``uv`` image are pinned by digest, the serving requirements install with ``--require-hashes``, the
model tree is checked at build time (root-owned, no symlink, nothing group- or world-writable, readable), setuid and setgid
bits are stripped before the user switch, the serving user has no home directory, and a build step run as that user fails
if it can write anything on the import path, under ``/srv`` or under ``/opt/semigraph``. The model stage is covered by
``test_dockerfile_embedder.py`` and is not touched here.
"""


import json
import posixpath
import re
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
WEB_FROM = re.compile(r"^FROM node:24\.22\.2-slim@sha256:[0-9a-f]{64} AS web$")
MODEL_COPY = "COPY --from=model /models /srv/models"    # no --chown: root-owned, read-only to the serving user
PYTHON_FROM = re.compile(r"^FROM python:3\.13-slim@sha256:([0-9a-f]{64})(?: AS model)?$")
UV_IMAGE = re.compile(r"^ghcr\.io/astral-sh/uv:[\d.]+@sha256:[0-9a-f]{64}$")
# The build fails here, not the first boot, when the model tree is not what the serving user needs: the default model file must
# exist; no entry may be a symlink, group- or world-writable or owned by anyone but root; and every entry (but the *.py probe
# scripts, which root runs) must be readable (files by others, directories also enterable). ``find`` runs in an assignment, so
# its own failure stops the build; the result is tested afterwards (``test -z "$(find ...)"`` would ignore a failing ``find``).
MODEL_READABLE = r"""RUN set -eu; test -f "$ONNX_MODEL_PATH"; bad="$(find /srv/models \( -type l -o -perm /022 -o ! -user root -o \( ! -name '*.py' \( \( -type f ! -perm -004 \) -o \( -type d ! -perm -005 \) \) \) \) -print -quit)"; test -z "$bad" || { echo "unsafe model tree entry: $bad" >&2; exit 1; }"""
# Run AS the serving user, after the user switch: the build fails if that user can write the import path (every sys.path entry
# and every parent of one), /srv or /opt/semigraph (each tree and every parent of it).
IMPORT_PATH_CHECK = r"""RUN set -eu; paths="$(python -c 'import sys; print(*sys.path)')"; for p in $paths /srv /opt/semigraph; do [ -e "$p" ] || continue; found="$(find "$p" -writable -print -quit)"; [ -z "$found" ] || { echo "writable by the serving user: $found" >&2; exit 1; }; while [ "$p" != / ]; do if [ -w "$p" ]; then echo "writable by the serving user: $p" >&2; exit 1; fi; p="$(dirname "$p")"; done; done"""
# Setuid and setgid bits come off every file of the image's root filesystem (the build fails if one is left); it only removes bits.
STRIP_SETID = """RUN set -eu; find / -xdev -perm /6000 -type f -exec chmod a-s {} +; bad="$(find / -xdev -perm /6000 -type f -print -quit)"; test -z "$bad\""""
USERADD = "RUN useradd --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app"
SERVE_INSTALL = "RUN uv pip install --system --no-cache --require-hashes -r deploy/requirements-serve.txt"
WORKDIR_SRV = "WORKDIR /srv"
SAFE_PATH = "PYTHONSAFEPATH=1"
NO_USER_SITE = "PYTHONNOUSERSITE=1"
CACHE_HOME = "XDG_CACHE_HOME=/tmp/.cache"        # the serving user has no home to cache in: caches go to /tmp
SCAN_EXEMPT = {STRIP_SETID}                      # the one step the ownership scans read as a hit (it names ``/``) and that only removes bits
DIST_TARGET = "/opt/semigraph/web"        # /opt and /opt/semigraph are root-owned and nothing chowns them (R13, owner decision 5)
WEB_COPY = f"COPY --from=web /web/dist {DIST_TARGET}"  # no --chown: root-owned, the app user cannot write it (R13)
WEB_COPY_TARGET = re.compile(r"^COPY --from=web /web/dist (\S+)$")
MODE_STEP = "RUN chmod -R u=rwX,go=rX /web/dist"       # owner rwX; group and others read (and enter directories), never write


KEYWORDS = {"FROM", "RUN", "COPY", "ADD", "USER", "WORKDIR", "ENV", "ARG", "CMD", "ENTRYPOINT", "EXPOSE", "LABEL", "VOLUME", "SHELL",
            "HEALTHCHECK", "STOPSIGNAL", "ONBUILD", "MAINTAINER"}


def keyword_upper(step: str) -> str:
    """The instruction keyword in upper case (Docker reads ``run`` and ``RUN`` alike); the rest of the line is left as written."""
    word, space, rest = step.partition(" ")
    return word.upper() + space + rest if word.upper() in KEYWORDS else step


def instructions(text: str) -> list[str]:
    """Dockerfile instructions with continuation lines joined, comments dropped and the keyword in upper case."""
    out, current = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not current and (not line or line.startswith("#")):
            continue
        if current and line.startswith("#"):
            continue
        current += (" " if current else "") + line.removesuffix("\\").strip()
        if not line.endswith("\\"):
            out.append(keyword_upper(current))
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
    assert PYTHON_FROM.match(STAGES["runtime"][0]), STAGES["runtime"][0]      # the last stage: the default build target


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


def test_the_built_app_is_copied_without_chown_so_the_serving_user_cannot_rewrite_it():
    """R13 amendment (owner, 2026-11-07). The web stage builds as root, so a COPY without ``--chown`` leaves the dist
    root-owned: ``app`` can read it and cannot write it. Since the 2026-11-07 security review nothing in the runtime stage is
    handed to ``app`` any more: no ``chown`` of ``/srv`` and no ``--chown`` on the model copy (see
    ``test_no_step_gives_the_app_user_write_access_to_the_working_directory_or_the_models``), and the dist stays outside
    ``/srv`` (``test_the_dist_is_outside_srv_under_parents_that_nothing_hands_to_the_app_user``: owner decision 5 stands).
    Nothing after the web copy may chown or chmod anything. Docker is not available to these tests: the ownership is pinned in
    the file, not measured in an image."""
    runtime = STAGES["runtime"]
    web_copies = [step for step in runtime if step.startswith("COPY --from=web ")]
    assert web_copies == [WEB_COPY]
    assert "--chown" not in web_copies[0]
    assert MODEL_COPY in runtime and "--chown" not in MODEL_COPY                 # the model layer stays root's as well
    assert [step for step in runtime if "chown" in step] == []
    web_copy = runtime.index(WEB_COPY)
    assert [step for step in runtime[web_copy + 1:] if ("chown" in step or "chmod" in step) and step not in SCAN_EXEMPT] == []


# ---------------------------------------------------------------- the dist location (R13 residual, owner decision 5)

OWNERSHIP_COMMANDS = {"chown", "chgrp", "chmod", "setfacl", "useradd", "usermod", "adduser"}
SEGMENT_BREAKS = re.compile(r"&&|\|\||;|\||\b(?:then|do|else)\b")
UNRESOLVABLE = "*?[$"


def web_copy_target() -> str:
    """The destination of the one COPY from the web stage, read from the Dockerfile (the one place the image path is written)."""
    targets = [m.group(1) for step in STAGES["runtime"] if (m := WEB_COPY_TARGET.match(step))]
    assert len(targets) == 1, targets
    return targets[0]


def exec_form(body: str) -> list[str] | None:
    """The argv of an exec-form ``RUN ["cmd", "arg"]`` body (a JSON array of strings), or None when it is not one."""
    try:
        argv = json.loads(body)
    except ValueError:
        return None
    return argv if isinstance(argv, list) and all(isinstance(word, str) for word in argv) else None


def ownership_paths(step: str) -> list[str]:
    """The absolute (or escaping) paths one instruction hands to a command that changes owner, group, mode or ACL, or that
    creates a user's home directory: a ``COPY``/``ADD`` carrying ``--chown``/``--chmod`` (its destination), and every
    path-like word of a ``RUN`` segment that names such a command (``find /opt -exec chown ...`` included). The keyword is read
    in any case, and an exec-form ``RUN [...]`` is read as its argv joined (one that is not valid JSON is returned as
    unreadable). A word with a glob character, a variable or a ``..`` is returned as well: reading the file cannot say where it
    points."""
    words = step.split()
    keyword = words[0].upper() if words else ""
    if keyword in ("COPY", "ADD"):
        return [words[-1]] if any(w.lower().startswith(("--chown", "--chmod")) for w in words) else []
    if keyword != "RUN":
        return []
    body = step.split(None, 1)[1] if len(words) > 1 else ""
    if body.startswith("["):
        argv = exec_form(body)
        if argv is None:
            return ["$unreadable"]
        body = " ".join(argv)
    out: list[str] = []
    for segment in SEGMENT_BREAKS.split(body):
        tokens = [w.strip("'\"()") for w in segment.split()]
        # ``install`` counts only as the command of a segment (``pip install`` is not the coreutils one)
        if tokens[:1] == ["install"] or any(w.rsplit("/", 1)[-1] in OWNERSHIP_COMMANDS for w in tokens):
            out += [w for w in tokens if w.startswith(("/", "..")) or "/.." in w or any(c in w for c in UNRESOLVABLE)]
    return out


def reaches_path(path: str, root: str) -> bool:
    """Whether a command given ``path`` can write ``root``: ``root`` itself, anything below it, any parent of it (write
    permission on a parent covers replacing ``root``), or a path read as unresolvable."""
    if any(c in path for c in UNRESOLVABLE) or ".." in path.split("/"):
        return True
    p, r = PurePosixPath(posixpath.normpath("/" + path.lstrip("/"))), PurePosixPath(root)
    return p == r or r in p.parents or p in r.parents


def reaches_the_dist(path: str, target: str) -> bool:
    """Whether a command given ``path`` can write the dist: the target itself, anything below it, any parent of it, anything
    under ``/opt``, or a path read as unresolvable."""
    p, opt = PurePosixPath(posixpath.normpath("/" + path.lstrip("/"))), PurePosixPath("/opt")
    return reaches_path(path, target) or p == opt or opt in p.parents


HEREDOC = "<<"
HEREDOC_STEP = re.compile(r"(?:run|copy|add)\s", re.IGNORECASE)


def heredoc_steps(text: str) -> list[str]:
    """The ``RUN``, ``COPY`` and ``ADD`` instructions of a Dockerfile text that open a heredoc: ``<<`` anywhere in the
    instruction (``RUN <<EOF``, ``RUN <<-EOT``, ``RUN cat <<'EOF' > file``, ``COPY <<EOF /dest``). The keyword is read in any
    case, as Docker does. A ``<<`` inside quotes or arithmetic is flagged as well: the guard fails closed."""
    return [step for step in instructions(text) if HEREDOC_STEP.match(step) and HEREDOC in step]


def test_the_dist_is_outside_srv_under_parents_that_nothing_hands_to_the_app_user():
    """R13 residual, closed by owner decision 5 (2026-11-07): the app is served from ``/opt/semigraph/web``, outside ``/srv``.
    Its parents, ``/opt`` and ``/opt/semigraph``, stay root-owned (the COPY creates the missing one as root; the base image's
    ``/opt`` is root's). ``/srv`` is root-owned too (H1, owner-approved 2026-11-07), and the dist stays where decision 5 put it:
    no step hands any of these paths to ``app``. Docker is not available to these tests: this pins the file, so the ownership
    itself is not measured in an image."""
    target = web_copy_target()
    assert target == DIST_TARGET == "/opt/semigraph/web"
    assert WEB_COPY in STAGES["runtime"]
    assert PurePosixPath("/srv") not in [PurePosixPath(target), *PurePosixPath(target).parents]
    # no instruction in the whole file (any stage, any position) but the web copy, and the build step that checks the serving user
    # cannot write that tree, so much as names it
    assert [step for steps in STAGES.values() for step in steps if "/opt" in step] == [WEB_COPY, IMPORT_PATH_CHECK]


def test_no_step_before_or_after_gives_the_app_user_write_access_to_the_dist_or_its_parents():
    """Every stage, every position. Since H1 (owner-approved 2026-11-07) no step of the runtime stage chowns anything: the
    commands the scan reads there are ``useradd`` (it creates no home directory; its path-like words are ``/nonexistent`` and the
    login shell ``/usr/sbin/nologin``) and the setuid strip (exempt by exact text, ``test_the_setid_strip_...``), and neither
    reaches ``/opt``, ``/opt/semigraph`` or the dist; the web stage's mode step touches only ``/web/dist``."""
    target = web_copy_target()
    reaching = [(name, step) for name, steps in STAGES.items() for step in steps if step not in SCAN_EXEMPT
                if any(reaches_the_dist(path, target) for path in ownership_paths(step))]
    assert reaching == []


@pytest.mark.parametrize("step", [
    "RUN chown -R app:app /opt/semigraph",
    "RUN chown app:app /opt",
    "RUN chmod -R 777 /opt",
    "RUN chmod go+w /opt/semigraph",
    "RUN chown -R app:app /",
    "RUN chown -R app:app /opt/semigraph/web",
    "RUN mkdir -p /opt/semigraph && chown app /opt/semigraph",
    "RUN find /opt -exec chown app {} +",
    "RUN chown -R app:app /srv/../opt",
    "RUN chown -R app:app /op*",
    "RUN chown -R app:app $DIST",
    "RUN sh -c \"chown -R app /opt\"",
    "RUN install -o app -d /opt/semigraph",
    "RUN useradd -m -d /opt/semigraph app",
    "COPY --chown=app:app x /opt/semigraph/",
    "ADD --chmod=777 x /opt/semigraph/web",
    "run chown -R app:app /opt/semigraph",
    "Run chmod -R 777 /opt",
    "copy --chown=app:app x /opt/semigraph/",
    "add --chmod=777 x /opt/semigraph/web",
    'RUN ["chown", "-R", "app:app", "/opt/semigraph"]',
    'RUN ["sh", "-c", "chmod -R 777 /opt"]',
    "RUN [chown, -R, app, /opt]",
])
def test_the_ownership_scan_catches_every_way_of_reaching_the_dist_or_its_parents(step):
    """The scan is only trustworthy if it flags what it exists for, so pin it on its own."""
    assert any(reaches_the_dist(path, DIST_TARGET) for path in ownership_paths(step)), step


OLD_USERADD = "RUN useradd --create-home --shell /usr/sbin/nologin app && chown -R app:app /srv"   # the earlier step, kept as text


@pytest.mark.parametrize("step", [
    USERADD,
    "RUN useradd --create-home --shell /usr/sbin/nologin app",
    OLD_USERADD,                      # no longer in the file; it never reached the dist at its new place
    "RUN chmod -R u=rwX,go=rX /web/dist",
    MODEL_COPY,
    f"COPY --from=web /web/dist {DIST_TARGET}",
    "RUN npm ci --ignore-scripts",
    WORKDIR_SRV,
])
def test_the_ownership_scan_does_not_flag_the_steps_the_file_needs(step):
    assert not any(reaches_the_dist(path, DIST_TARGET) for path in ownership_paths(step)), step


def test_the_scan_is_what_rules_out_the_old_location():
    """The reason for the move, stated as a check: with the dist under ``/srv`` the earlier ``chown -R app:app /srv`` step
    reaches it (that step is kept here as text, since the file no longer holds it)."""
    assert any(reaches_the_dist(path, "/srv/web") for path in ownership_paths(OLD_USERADD))
    assert not any(reaches_the_dist(path, DIST_TARGET) for path in ownership_paths(OLD_USERADD))


# ---------------------------------------------------------------- heredocs in RUN, COPY and ADD (round-3 review, INFO; round 5)

HEREDOC_BODY = "chown -R app:app /"          # a body line the ownership scan would have to see


def test_no_instruction_uses_a_heredoc():
    """``instructions()`` joins continuation lines and does not read a ``RUN <<EOF`` (or ``COPY <<EOF``) body: each body line becomes an entry of
    its own that does not start with ``RUN``, and ``ownership_paths`` returns nothing for it, so a body line such as ``chown -R
    app:app /`` would escape every ownership scan in this file (``test_a_heredoc_body_escapes_the_ownership_scan_...`` shows it).
    Reading heredoc bodies would be new code to trust, so the file fails closed instead: the Dockerfile has no heredoc today, and
    one added later fails here, where its author learns that the scans do not look inside it (round-3 review, 2026-11-07, INFO)."""
    assert heredoc_steps(DOCKERFILE.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("text", [
    pytest.param(f"FROM python:3.13-slim\nRUN <<EOF\n{HEREDOC_BODY}\nEOF\nUSER app\n", id="run-heredoc"),
    pytest.param(f"FROM python:3.13-slim\nRUN <<-EOT\n\t{HEREDOC_BODY}\n\tEOT\nUSER app\n", id="run-dash-heredoc-tab-indented"),
    pytest.param(f"RUN <<'EOF'\n{HEREDOC_BODY}\nEOF\n", id="run-quoted-delimiter"),
    pytest.param(f"RUN cat <<EOF > /etc/motd\n{HEREDOC_BODY}\nEOF\n", id="heredoc-after-a-command"),
    pytest.param(f"RUN set -e \\\n && cat <<EOF > /etc/motd\n{HEREDOC_BODY}\nEOF\n", id="heredoc-on-a-continuation-line"),
    pytest.param(f"run <<EOF\n{HEREDOC_BODY}\nEOF\n", id="lowercase-keyword"),
    pytest.param(f"COPY <<EOF /srv/x\n{HEREDOC_BODY}\nEOF\n", id="copy-heredoc"),
    pytest.param(f"COPY --chmod=0755 <<'EOF' /usr/local/bin/x\n{HEREDOC_BODY}\nEOF\n", id="copy-heredoc-with-flags"),
    pytest.param(f"ADD <<EOF /srv/x\n{HEREDOC_BODY}\nEOF\n", id="add-heredoc"),
    pytest.param(f"copy <<EOF /srv/x\n{HEREDOC_BODY}\nEOF\n", id="lowercase-copy-heredoc"),
])
def test_the_heredoc_guard_flags_a_heredoc_instruction(text):
    """The guard reads the Dockerfile text through ``instructions()`` (the joiner the real test uses), not a single step."""
    assert len(heredoc_steps(text)) == 1, text


@pytest.mark.parametrize("text", [
    pytest.param("RUN chown -R app:app /srv\n", id="ordinary-run-even-a-bad-one"),
    pytest.param("RUN uv pip install --system --no-cache -r deploy/requirements-serve.txt\n", id="ordinary-run"),
    pytest.param(f"{MODEL_READABLE}\n", id="the-model-readability-step"),
    pytest.param("RUN set -e \\\n && echo ok\n", id="continuation-lines"),
    pytest.param("COPY src/ src/\n", id="ordinary-copy"),
    pytest.param("ADD --chown=app x /opt/y\n", id="ordinary-add"),
    pytest.param("LABEL note=\"1 << 3\"\n", id="not-a-run-even-with-a-shift"),
    pytest.param("# RUN <<EOF\nRUN true\n", id="a-comment"),
])
def test_the_heredoc_guard_does_not_flag_an_ordinary_instruction(text):
    assert heredoc_steps(text) == [], text


def test_a_heredoc_body_escapes_the_ownership_scan_which_is_why_the_guard_exists():
    text = f"FROM python:3.13-slim\nRUN <<EOF\n{HEREDOC_BODY}\nEOF\nUSER app\n"
    steps = instructions(text)
    assert HEREDOC_BODY in steps                                                   # the body line is an entry of its own ...
    assert [path for step in steps for path in ownership_paths(step)] == []        # ... that no scan reads
    assert heredoc_steps(text) == ["RUN <<EOF"]                                     # the guard sees the opener
    assert any(reaches_path(path, "/srv") for path in ownership_paths(f"RUN {HEREDOC_BODY}"))   # one line: the scan catches it


# ---------------------------------------------------------------- the working directory and the models (I1 security review, 2026-11-07)

@pytest.mark.parametrize("step", [
    "RUN chown -R app:app /srv",
    "RUN chown app /srv",
    "RUN chown -R app:app /srv/models",
    "RUN chmod -R go+w /srv",
    "RUN chmod 777 /srv/models",
    "RUN chown -R app:app /",
    "RUN chown -R app:app /sr*",
    "RUN chown -R app:app $WORK",
    "RUN chown -R app:app /srv/../srv",
    "RUN useradd -m -d /srv app",
    "RUN find /srv -exec chown app {} +",
    "RUN install -o app -d /srv/state",
    "COPY --from=model --chown=app:app /models /srv/models",
    "COPY --chown=app:app src/ /srv/src/",
    "ADD --chmod=777 x /srv/x",
    "run chown -R app:app /srv",
    "copy --from=model --chown=app:app /models /srv/models",
    'RUN ["chown", "-R", "app:app", "/srv"]',
    'RUN ["sh", "-c", "chown -R app /srv/models"]',
    "RUN [chown, -R, app, /srv]",
    OLD_USERADD,
])
def test_the_srv_scan_catches_every_way_of_handing_the_working_directory_or_the_models_to_a_user(step):
    assert any(reaches_path(path, "/srv") for path in ownership_paths(step)), step


@pytest.mark.parametrize("step", [
    USERADD,
    "RUN useradd --create-home --shell /usr/sbin/nologin app",
    MODEL_COPY,
    "COPY src/ src/",
    "COPY pyproject.toml README.md ./",
    "RUN chmod -R u=rwX,go=rX /web/dist",
    f"COPY --from=web /web/dist {DIST_TARGET}",
    WORKDIR_SRV,
])
def test_the_srv_scan_does_not_flag_the_steps_the_file_needs(step):
    assert not any(reaches_path(path, "/srv") for path in ownership_paths(step)), step


def test_no_step_gives_the_app_user_write_access_to_the_working_directory_or_the_models():
    """The working directory and the models are root-owned. The server and every upload worker
    start in ``/srv`` and ``Settings`` reads ``.env`` from the working directory, so nothing there is handed to the serving user:
    no ``chown`` of ``/srv`` and no ``--chown`` on the model copy. The serving code writes nothing under ``/srv`` (it keeps its
    state in the database and its temporary files in ``/tmp``), so nothing needs to stay writable: no step of any stage reaches
    ``/srv``, its parents or anything below it."""
    reaching = [(name, step) for name, steps in STAGES.items() for step in steps if step not in SCAN_EXEMPT
                if any(reaches_path(path, "/srv") for path in ownership_paths(step))]
    assert reaching == []


def test_the_server_starts_in_srv_which_the_image_creates_as_root():
    """The working directory of ``CMD`` (and so of every child the server starts: none sets ``cwd=``) is the last ``WORKDIR``
    before the user switch. ``WORKDIR`` makes the directory as root, and the scan above rules out any step that hands it on."""
    runtime = STAGES["runtime"]
    before_user = runtime[: runtime.index("USER app")]
    assert [step for step in before_user if step.startswith("WORKDIR ")][-1] == WORKDIR_SRV
    assert runtime[-1].startswith("CMD ") and runtime.index("USER app") < len(runtime) - 1


def test_the_runtime_stage_takes_the_working_directory_and_the_user_site_off_the_import_path():
    """``PYTHONSAFEPATH=1`` (Python 3.11+) keeps the working directory off ``sys.path`` for the server (``python -m ...`` puts the
    cwd first on it) and ``PYTHONNOUSERSITE=1`` keeps the user site off it (the serving user has no home directory since commit 8;
    the setting stays as a second layer). The sandboxed workers do not inherit them (their environment is an allowlist), so they carry ``-P`` and ``-s`` on their
    own argv (``tests/test_sandbox_safe_path.py``). Both are in an ``ENV`` instruction of their own, after the first one, which
    the embedder test reads for ``ONNX_MODEL_PATH``."""
    runtime = STAGES["runtime"]
    envs = [step for step in runtime if step.startswith("ENV ")]
    for setting in (SAFE_PATH, NO_USER_SITE):
        assert any(setting in step.split() for step in envs), (setting, envs)
        assert runtime.index(next(step for step in envs if setting in step.split())) < runtime.index("USER app"), setting
        assert not any(setting in step for name in ("model", "web") for step in STAGES[name])   # those stages run no Python for the app


def test_the_model_tree_is_checked_at_build_time():
    """Without ``--chown`` the model files keep the owner and mode they had in the model stage. A mode that left them
    unreadable to ``app`` would otherwise show only at the first boot, and a symlink or a group- or world-writable entry would
    leave the models open to change: this step fails the image build instead. It covers every model directory under
    ``/srv/models`` (a ``KEEP_UNPATCHED`` image holds the 8-bit model beside the fp32 one, and a staging app may serve either),
    not only the default model's, and skips only the readability test of the ``*.py`` probe scripts, which root runs. It sits
    right after the model copy and before the user switch, so it runs as root against exactly the files that were copied."""
    runtime = STAGES["runtime"]
    assert MODEL_READABLE in runtime
    assert runtime.index(MODEL_COPY) < runtime.index(MODEL_READABLE) < runtime.index("USER app")


def test_the_model_check_tests_the_find_result_after_assigning_it_and_refuses_links_and_writable_entries():
    """``test -z "$(find ...)"`` is true when ``find`` itself fails (an unreadable directory, a missing path), so the check
    would pass on an error. The output is assigned first (an assignment carries ``find``'s exit status, and ``set -e`` stops on
    it) and tested afterwards. The expression refuses a symlink, any group- or world-writable entry and any owner but root."""
    check = MODEL_READABLE
    assert check.startswith("RUN set -eu;") and 'bad="$(find /srv/models ' in check and 'test -z "$bad"' in check
    assert 'test -z "$(find' not in check
    for refused in ("-type l", "-perm /022", "! -user root"):
        assert refused in check, refused
    assert re.search(r"\) -print -quit\)\"", check), "-print -quit must apply to the whole OR group"
    assert 'test -f "$ONNX_MODEL_PATH"' in check and check.index("test -f") < check.index("bad=")


def test_a_build_step_run_by_the_serving_user_checks_the_import_path_and_the_trees_it_serves_from():
    """After ``USER app`` (so ``-w`` and ``find -writable`` answer for the serving user, not for root) the build fails if that user
    can write any ``sys.path`` entry, ``/srv`` or ``/opt/semigraph``, or any parent directory of one of them. The path list is
    assigned first so a failing ``python`` stops the step. Not measured here: no Docker, so the I4 image build is the proof."""
    runtime = STAGES["runtime"]
    assert IMPORT_PATH_CHECK in runtime
    assert runtime.index("USER app") < runtime.index(IMPORT_PATH_CHECK) < runtime.index(runtime[-1])
    assert runtime[-1].startswith("CMD ")
    for needed in ("sys.path", "/srv", "/opt/semigraph", "-writable", '[ -w "$p" ]', 'dirname "$p"', "exit 1"):
        assert needed in IMPORT_PATH_CHECK, needed
    assert IMPORT_PATH_CHECK.index('paths="$(python') < IMPORT_PATH_CHECK.index("for p in")


def test_the_setid_strip_runs_before_the_user_switch_and_only_removes_bits():
    """Setuid and setgid bits come off every file of the root filesystem after the last thing is installed or copied, and the
    step fails if one is left. It names ``/`` and ``chmod``, so the ownership scans read it as a hit; they skip it by its exact
    text, which only removes bits (``a-s``): the variants below are not skipped and are flagged."""
    runtime = STAGES["runtime"]
    assert STRIP_SETID in runtime
    installs = [i for i, step in enumerate(runtime) if step.startswith(("COPY ", "RUN uv pip install"))]
    assert max(installs) < runtime.index(STRIP_SETID) < runtime.index("USER app")
    assert "chmod a-s " in STRIP_SETID and "-perm /6000" in STRIP_SETID and "-xdev" in STRIP_SETID
    for variant in (STRIP_SETID.replace("a-s", "a+rwx"), STRIP_SETID.replace("a-s", "u+s"), STRIP_SETID.replace("chmod a-s", "chown app")):
        assert variant not in SCAN_EXEMPT
        assert any(reaches_path(path, "/srv") for path in ownership_paths(variant)), variant
    assert any(reaches_path(path, "/srv") for path in ownership_paths(STRIP_SETID))     # the scan does read it; the exemption is by text


def test_the_serving_user_has_no_home_directory_and_its_caches_go_to_tmp():
    """The user is created without a home (``/nonexistent``), so there is no home directory to write; caches are sent to ``/tmp``
    by ``XDG_CACHE_HOME``, which sits in the ``ENV`` before the user switch."""
    runtime = STAGES["runtime"]
    assert USERADD in runtime and "--create-home" not in "\n".join(runtime)
    env = next(step for step in runtime if CACHE_HOME in step.split())
    assert env.startswith("ENV ") and runtime.index(env) < runtime.index("USER app")
    assert not any(CACHE_HOME in step for name in ("model", "web") for step in STAGES[name])


def workdirs_after_user(steps: list[str]) -> list[str]:
    """The ``WORKDIR`` instructions after the first ``USER``: Docker makes such a directory owned by the user that is current
    then, so it would hand a directory to the serving user."""
    users = [i for i, step in enumerate(steps) if step.startswith("USER ")]
    return [step for step in steps[users[0] + 1:] if step.startswith("WORKDIR ")] if users else []


@pytest.mark.parametrize("text, expected", [
    pytest.param("USER app\nWORKDIR /srv\n", ["WORKDIR /srv"], id="workdir-after-user"),
    pytest.param("USER app\nRUN true\nworkdir /x\n", ["WORKDIR /x"], id="lowercase-workdir-after-user"),
    pytest.param("WORKDIR /srv\nUSER app\nCMD [\"x\"]\n", [], id="workdir-before-user"),
    pytest.param("WORKDIR /srv\nCMD [\"x\"]\n", [], id="no-user"),
])
def test_the_workdir_scan_flags_a_workdir_after_the_user_switch(text, expected):
    assert workdirs_after_user(instructions(text)) == expected


def test_no_workdir_follows_the_user_switch():
    assert workdirs_after_user(STAGES["runtime"]) == []


def test_the_base_images_are_pinned_by_digest_and_the_serving_requirements_install_with_hashes():
    """The model stage and the runtime stage start from the same ``python:3.13-slim`` index digest, and ``uv`` is copied from an
    image pinned by digest (a tag can move). The requirements file is hash-locked (``tests/test_deploy_hashes.py``) and the
    install states ``--require-hashes`` on its own command line, so a package without a pinned hash fails the build."""
    model, runtime = PYTHON_FROM.match(STAGES["model"][0]), PYTHON_FROM.match(STAGES["runtime"][0])
    assert model and runtime and model.group(1) == runtime.group(1)
    assert SERVE_INSTALL in STAGES["runtime"]
    assert not [step for step in STAGES["runtime"] if "uv pip install" in step and "--require-hashes" not in step and "--no-deps ." not in step]
    uv_copies = [step for step in STAGES["runtime"] if step.startswith("COPY --from=ghcr.io/")]
    assert len(uv_copies) == 1 and UV_IMAGE.fullmatch(uv_copies[0].split()[1].removeprefix("--from="))


def test_the_lowercase_and_exec_form_steps_are_read_by_the_instruction_parser():
    steps = instructions("from python:3.13-slim\nrun chown -R app /srv\nRUN [\"chown\", \"app\", \"/srv\"]\nuser app\n")
    assert steps[:2] == ["FROM python:3.13-slim", "RUN chown -R app /srv"] and steps[-1] == "USER app"
    assert [any(reaches_path(path, "/srv") for path in ownership_paths(step)) for step in steps[1:3]] == [True, True]


def test_the_web_stage_sets_the_dist_modes_after_the_build_and_never_chowns_it():
    """Vite copies ``web/public`` with a file copy that keeps the mode of the file in the build context, so a context made under
    a restrictive umask (a developer machine running ``fly deploy``) can put a root-owned 0600 ``favicon.svg`` into the dist: a
    file ``app`` cannot read (``lookup_path`` only stats it, so its response would start as 200 and fail mid-body). The mode
    step comes right after the build: owner rwX, group and others read-only. It does not change the owner (root's)."""
    web = STAGES["web"]
    assert MODE_STEP in web
    assert web.index("RUN npm run build") < web.index(MODE_STEP)
    assert "chown" not in "\n".join(web)


def test_the_runtime_stage_has_no_node_binary_step():
    """The runtime image takes exactly three things from elsewhere: the ``uv`` binary, the model layer and the built app. In
    particular nothing copies a Node binary (or anything but ``/web/dist``) out of the web stage."""
    runtime = "\n".join(STAGES["runtime"])
    uv, *rest = sorted(re.findall(r"--from=(\S+)", runtime))      # the order is pinned above; any uv version, so a bump stays green
    assert UV_IMAGE.fullmatch(uv) and rest == ["model", "web"]
    assert re.findall(r"--from=web (\S+) ", runtime) == ["/web/dist"]
    assert not re.search(r"/(?:usr/(?:local/)?)?bin/node\b|\bnode(?:js)?\b", runtime, re.IGNORECASE)


def test_the_dist_lands_where_the_settings_default_looks_for_it():
    """The one cross-file check. Python spells the path once (``config.UI_DIST_DIR_DEFAULT``; the ``Settings`` default and
    ``ui_v2.DEFAULT_DIST_DIR`` are that very object, pinned in ``test_serve_ui_v2.py``), and the Dockerfile's web COPY target is
    its only other spelling. Neither is compared with a constant of this file alone, so a default that drifts from the image
    (or an image that drifts from the default) fails here; the literal below pins the agreed path as well."""
    from semigraph.config import UI_DIST_DIR_DEFAULT, Settings
    from semigraph.serve.ui_v2 import DEFAULT_DIST_DIR

    target = web_copy_target()
    assert UI_DIST_DIR_DEFAULT.as_posix() == target
    assert Settings.model_fields["ui_dist_dir"].default.as_posix() == target
    assert DEFAULT_DIST_DIR.as_posix() == target
    assert UI_DIST_DIR_DEFAULT == Path("/opt/semigraph/web")


def test_the_runtime_stage_has_no_node_npm_or_web_sources():
    runtime = STAGES["runtime"]
    text = "\n".join(runtime).lower()
    for forbidden in ("node", "npm", "npx", "package.json"):
        assert forbidden not in text, forbidden
    assert re.findall(r"--from=web (\S+)", "\n".join(runtime)) == ["/web/dist"]       # no flag between the stage and the path


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


# ---------------------------------------------------------------- the build context (I1 security review: S10)

def pattern_regex(pattern: str) -> re.Pattern[str]:
    """One dockerignore pattern as a regex over a slash-separated path from the context root: ``**/`` is any number of
    directories (including none), ``*`` and ``?`` never cross a ``/``, everything else is literal. The patterns are
    anchored at the root, which is the whole point of the nested ``**/`` rules below."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(f"^{out}$")


def is_excluded(path: str) -> bool:
    """Whether the build context drops ``path``: a rule matches the path or any parent directory of it, the last
    matching rule wins, and a leading ``!`` makes a rule re-include (Docker's documented behaviour)."""
    parts = path.split("/")
    candidates = ["/".join(parts[: n + 1]) for n in range(len(parts))]
    excluded = False
    for rule in ignored_lines():
        negated = rule.startswith("!")
        regex = pattern_regex(rule.removeprefix("!").strip("/"))
        if any(regex.match(candidate) for candidate in candidates):
            excluded = not negated
    return excluded


def test_the_matcher_follows_the_documented_rules():
    """The helper is only trustworthy if it reproduces the behaviour the env rules rely on, so pin it on its own."""
    assert pattern_regex("**/.env").match(".env") and pattern_regex("**/.env").match("a/b/.env")
    assert not pattern_regex(".env").match("web/.env")                 # a bare pattern is anchored at the root
    assert not pattern_regex("web/dist").match("web/dist/x")           # parents are handled by is_excluded, not the regex
    assert pattern_regex("web/*.log").match("web/a.log") and not pattern_regex("web/*.log").match("web/a/b.log")


@pytest.mark.parametrize("path", [".env", ".env.fly", ".env.community", "web/.env", "web/.env.local", "web/.env.production",
                                  "web/.env.development.local", "web/src/.env", "deploy/staging/.env.loadgen"])
def test_dockerignore_keeps_every_env_file_at_any_depth_out_of_the_context(path):
    """Vite inlines every ``VITE_*`` value of a ``web/.env*`` into the public bundle, and the root-anchored ``.env.*`` rules
    do not match ``web/.env.production``: the web stage copies all of ``web/``, so the nested rules must exist."""
    assert is_excluded(path), path


@pytest.mark.parametrize("path", [".env.example", "web/.env.example"])
def test_dockerignore_still_lets_the_env_example_files_in(path):
    assert not is_excluded(path), path


def test_dockerignore_env_rules_are_nested_and_the_example_exception_comes_last():
    lines = [ln.strip() for ln in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()]
    for rule in ("**/.env", "**/.env.*", "!**/.env.example"):
        assert rule in lines, rule
    assert lines.index("**/.env.*") < lines.index("!**/.env.example")      # the last matching rule wins


@pytest.mark.parametrize("path", ["web/coverage/index.html", "web/test-results/.last-run.json", "web/playwright-report/index.html",
                                  "web/lighthouse-out/report.json", "web/node_modules/vite/package.json", "web/dist/index.html"])
def test_dockerignore_keeps_the_web_tool_output_out_of_the_context(path):
    assert is_excluded(path), path


def test_the_context_still_holds_everything_the_web_stage_copies():
    needed = ("web/package.json", "web/package-lock.json", "web/.npmrc", "web/index.html", "web/src/main.tsx",
              "web/src/lib/ids.generated.ts", "web/vite.config.ts", "web/tsconfig.json", "web/.node-version")
    assert [path for path in needed if is_excluded(path)] == []
