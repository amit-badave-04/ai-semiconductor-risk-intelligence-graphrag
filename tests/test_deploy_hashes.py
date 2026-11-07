"""Image hardening of the deploy inputs (round 5, commit 8; owner-approved 2026-11-07).

Docker is not available to these tests, so they pin the files: the serving requirements are hash-locked (every pin carries
``--hash`` lines, the header command says ``--generate-hashes``), and the three staging images (``deploy/staging/Dockerfile.*``)
hand the serving user only the output directory ``/out`` (no ``chown`` of the working directory), take the working directory and
the user site off the import path (``PYTHONSAFEPATH=1``, ``PYTHONNOUSERSITE=1``) and keep their code importable through an
explicit root-owned ``PYTHONPATH``. The main ``Dockerfile`` is pinned by ``test_dockerfile_web.py``; the I4 image build is the
proof of all of it.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "deploy" / "requirements-serve.txt"
REQUIREMENTS_IN = ROOT / "deploy" / "requirements-serve.in"
STAGING = ROOT / "deploy" / "staging"
COMPILE = "uv pip compile deploy/requirements-serve.in -o deploy/requirements-serve.txt --python-version 3.13 --python-platform x86_64-manylinux_2_28 --generate-hashes"
PIN = re.compile(r"^([A-Za-z0-9_.\-]+)==([^\s;\\]+)( ;.*)? \\$")
HASH = re.compile(r"^    --hash=sha256:[0-9a-f]{64}( \\)?$")
SAFE_ENV = ("PYTHONSAFEPATH=1", "PYTHONNOUSERSITE=1")
STAGING_FILES = {
    "Dockerfile.mockllm": "/srv",
    "Dockerfile.tools": "/srv",
    "Dockerfile.loadgen": "/app",
}


def requirement_blocks(text: str) -> dict[str, list[str]]:
    """Package name to the lines under its pin line (hashes and ``# via`` comments), for every ``name==version`` line."""
    blocks: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        match = PIN.match(line)
        if match:
            current = match.group(1)
            blocks[current] = []
        elif current is not None and line.startswith("    "):
            blocks[current].append(line)
        elif line and not line.startswith("#"):
            raise AssertionError(f"line that is neither a pin, a hash nor a comment: {line!r}")
    return blocks


def test_the_header_names_the_command_that_generates_the_hashes():
    lines = REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    assert lines[1].strip() == "#    " + COMPILE.strip() or lines[1].strip() == "# " + COMPILE
    assert COMPILE in REQUIREMENTS_IN.read_text(encoding="utf-8")


def test_every_pin_carries_hashes():
    blocks = requirement_blocks(REQUIREMENTS.read_text(encoding="utf-8"))
    assert len(blocks) > 50
    for name, lines in blocks.items():
        hashes = [line for line in lines if line.lstrip().startswith("--hash=")]
        assert hashes, f"{name} has no --hash line"
        assert all(HASH.match(line) for line in hashes), name
    assert {"fastapi", "uvicorn", "onnxruntime", "numpy", "litellm", "neo4j"} <= set(blocks)


def test_the_hash_lock_is_not_loose_anywhere():
    text = REQUIREMENTS.read_text(encoding="utf-8")
    assert not re.search(r"^\s*(-e |git\+|https?://)", text, re.MULTILINE), "no editable, VCS or URL requirement"
    assert not re.search(r"^[A-Za-z0-9_.\-]+(>=|<=|~=|>|<)", text, re.MULTILINE), "every requirement is an exact pin"


def test_the_lock_is_resolved_for_the_linux_image_not_for_the_host_that_compiled_it():
    """The image is Linux. A lock compiled on a Windows host carries ``colorama`` and lacks ``uvloop`` (``uvicorn[standard]`` on
    Linux), so under ``--require-hashes`` the build would refuse the missing pin or run a different set than the one reviewed."""
    text = REQUIREMENTS.read_text(encoding="utf-8")
    assert "--python-platform x86_64-manylinux_2_28" in text.splitlines()[1]
    blocks = requirement_blocks(text)
    assert "uvloop" in blocks
    assert "colorama" not in blocks


def instructions(text: str) -> list[str]:
    """Dockerfile instructions with continuation lines joined and comments dropped (keyword in upper case)."""
    out, current = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not current and (not line or line.startswith("#")):
            continue
        if current and line.startswith("#"):
            continue
        current += (" " if current else "") + line.removesuffix("\\").strip()
        if not line.endswith("\\"):
            word, space, rest = current.partition(" ")
            out.append(word.upper() + space + rest if word.upper() in {"RUN", "COPY", "ADD", "ENV", "USER", "WORKDIR", "FROM", "CMD"} else current)
            current = ""
    return out


def staging(name: str) -> list[str]:
    return instructions((STAGING / name).read_text(encoding="utf-8"))


def chown_targets(step: str) -> list[str]:
    """The words after each ``chown`` in a RUN step that look like paths."""
    out = []
    for segment in re.split(r"&&|\|\||;", step):
        words = segment.split()
        if "chown" in words:
            out += [w for w in words[words.index("chown") + 1:] if w.startswith("/")]
    return out


@pytest.mark.parametrize("name, workdir", STAGING_FILES.items())
def test_a_staging_image_hands_the_serving_user_only_the_output_directory(name, workdir):
    """The working directory stays root-owned (no ``chown`` of it, no ``-R``); ``/out`` is the one directory the user owns, where
    the runs write their raw files (``mockllm`` writes nothing and owns nothing)."""
    steps = staging(name)
    chowns = [step for step in steps if "chown" in step]
    for step in chowns:
        assert chown_targets(step) == ["/out"], step
        assert "chown -R" not in step and "chown -r" not in step.lower(), step
    assert all("--chown" not in step for step in steps)
    if name == "Dockerfile.mockllm":
        assert chowns == []
    else:
        assert len(chowns) == 1 and "mkdir -p /out" in chowns[0], chowns
        assert steps.index(chowns[0]) < steps.index("USER app")


@pytest.mark.parametrize("name, workdir", STAGING_FILES.items())
def test_a_staging_image_takes_the_working_directory_and_the_user_site_off_the_import_path(name, workdir):
    steps = staging(name)
    envs = [step for step in steps if step.startswith("ENV ")]
    user = steps.index("USER app")
    for setting in SAFE_ENV:
        env = next((step for step in envs if setting in step.split()), None)
        assert env is not None, (name, setting)
        assert steps.index(env) < user, (name, setting)
    assert [step for step in steps[user + 1:] if step.startswith("WORKDIR ")] == []
    assert f"WORKDIR {workdir}" in steps


@pytest.mark.parametrize("name, workdir", STAGING_FILES.items())
def test_the_code_stays_importable_through_an_explicit_pythonpath(name, workdir):
    """``PYTHONSAFEPATH=1`` removes the working directory from ``sys.path``, and ``python -m tools...`` found the ``tools``
    package there. ``PYTHONPATH`` names the (root-owned) code directory instead, in an ``ENV`` before the user switch."""
    steps = staging(name)
    env = next(step for step in steps if step.startswith("ENV ") and f"PYTHONPATH={workdir}" in step.split())
    assert steps.index(env) < steps.index("USER app")


@pytest.mark.parametrize("name, workdir", STAGING_FILES.items())
def test_a_staging_image_sets_the_code_modes_so_the_serving_user_can_read_it_without_owning_it(name, workdir):
    """Nothing is chowned any more, so the files keep the mode of the build context (a context made under a restrictive umask
    would give the user unreadable files). One mode step after the last copy: owner rwX, group and others read only."""
    steps = staging(name)
    mode = f"RUN chmod -R u=rwX,go=rX {workdir}"
    assert mode in steps
    copies = [i for i, step in enumerate(steps) if step.startswith("COPY ") and "--from=" not in step]
    assert max(copies) < steps.index(mode) < steps.index("USER app")


def test_the_tools_image_installs_the_serving_requirements_with_hashes():
    steps = staging("Dockerfile.tools")
    install = "RUN uv pip install --system --no-cache --require-hashes -r deploy/requirements-serve.txt"
    assert install in steps
    assert [s for s in steps if "uv pip install" in s and "--require-hashes" not in s and "--no-deps ." not in s] == []


def test_the_staging_scan_helpers_catch_what_they_are_for():
    assert chown_targets("RUN useradd app && mkdir -p /out && chown app:app /out") == ["/out"]
    assert chown_targets("RUN chown -R app:app /srv /out") == ["/srv", "/out"]
    assert instructions("run chown -R app /srv\nuser app\n") == ["RUN chown -R app /srv", "USER app"]
    with pytest.raises(AssertionError):
        requirement_blocks("fastapi==0.141.0\n")          # a pin without the continuation that carries hashes
    assert json.dumps(sorted(requirement_blocks("a==1 \\\n    --hash=sha256:" + "0" * 64 + "\n"))) == '["a"]'
