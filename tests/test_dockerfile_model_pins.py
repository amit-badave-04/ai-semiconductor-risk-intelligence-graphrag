"""Dockerfile model stage: every ``pip install`` is hash-locked (R5-2, docs/security/accepted-risks.md).

Docker is not available to these tests, so they pin the files. The model stage installs from ``deploy/requirements-model.txt``
(the 8-bit build) and, for the fp32 variant, ``deploy/requirements-model-fp32.txt``, both with ``pip install --require-hashes -r``;
each file is copied in before the step that reads it; every requirement block of both files carries sha256 hashes; and every
package a model file shares with ``deploy/requirements-serve.txt`` is pinned at the serving version (the build's bit-exactness
check runs on the library the image serves with). The image build in CI is the proof of the install itself.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
DOCKERFILE = ROOT / "Dockerfile"
MODEL_FILES = ("requirements-model.txt", "requirements-model-fp32.txt")
BUILD_FILE = "requirements-build.txt"
PLATFORM_FLAGS = "--python-version 3.13 --python-platform x86_64-manylinux_2_28 --generate-hashes"
PIN = re.compile(r"^([A-Za-z0-9_.\-]+)==([^\s;\\]+)( ;.*)? \\$")
HASH = re.compile(r"^\s+--hash=sha256:[0-9a-f]{64}( \\)?$")
HASHED_INSTALL = re.compile(r"\bpip install --require-hashes --only-binary=:all: -r (deploy/requirements-model(?:-fp32)?\.txt)(?=\s*(?:;|&&|$))")
FP32_SHARED = ("onnxruntime", "numpy", "tokenizers")


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


def model_stage(text: str) -> list[str]:
    steps = instructions(text)
    froms = [i for i, step in enumerate(steps) if step.startswith("FROM ")]
    assert steps[froms[0]].endswith(" AS model")
    return steps[froms[0] + 1:froms[1]]


def requirement_blocks(path: Path) -> dict[str, tuple[str, list[str]]]:
    """Package name to (version, the lines under its pin line). A line that is not a pin, an indented line or a comment fails."""
    blocks: dict[str, tuple[str, list[str]]] = {}
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        match = PIN.match(line)
        if match:
            current = match.group(1)
            blocks[current] = (match.group(2), [])
        elif current is not None and line.startswith("    "):
            blocks[current][1].append(line)
        elif line and not line.startswith("#"):
            raise AssertionError(f"{path.name}: line that is neither a pin, a hash nor a comment: {line!r}")
    return blocks


def pip_commands(step: str) -> list[str]:
    """Every ``pip install`` command of one instruction (up to the next shell separator)."""
    return re.findall(r"\bpip install[^;&]*", step)


MODEL = model_stage(DOCKERFILE.read_text(encoding="utf-8"))
SERVE = requirement_blocks(DEPLOY / "requirements-serve.txt")


def test_every_pip_install_of_the_model_stage_is_hashed_and_reads_a_file_copied_in_before():
    installs = [(i, cmd) for i, step in enumerate(MODEL) for cmd in pip_commands(step)]
    assert len(installs) == 2, installs                       # the 8-bit build and the fp32 branch
    for index, command in installs:
        assert HASHED_INSTALL.search(command + " "), command  # --require-hashes -r <file>, nothing else on the line
        assert not re.search(r"""["'<>=~]""", command.replace("--only-binary=:all:", "")), command
        target = HASHED_INSTALL.search(command + " ").group(1)
        copies = [i for i, step in enumerate(MODEL) if step.startswith("COPY ") and target in step.split()]
        assert copies and min(copies) < index, f"{target} must be copied in before the step that installs it"
    assert {HASHED_INSTALL.search(cmd + " ").group(1) for _, cmd in installs} == {f"deploy/{n}" for n in MODEL_FILES}


def test_the_fp32_install_comes_after_the_8_bit_build_so_editing_it_never_invalidates_that_layer():
    build = next(i for i, step in enumerate(MODEL) if "scripts/build_onnx_embedder.py --out" in step)
    fp32 = next(i for i, step in enumerate(MODEL) if "requirements-model-fp32.txt" in step)
    assert build < fp32


@pytest.mark.parametrize("name", MODEL_FILES)
def test_every_requirement_block_of_a_model_file_is_hashed(name):
    blocks = requirement_blocks(DEPLOY / name)
    assert len(blocks) > 10
    for package, (_version, lines) in blocks.items():
        hashes = [line for line in lines if line.lstrip().startswith("--hash=")]
        assert hashes, f"{name}: {package} has no --hash line"
        assert all(HASH.match(line) for line in hashes), f"{name}: {package} has a malformed hash line"
        stray = [l for l in lines if not l.lstrip().startswith(("--hash=", "#"))]
        assert not stray, f"{name}: {package} has a line that is neither a hash nor a comment: {stray!r}"


@pytest.mark.parametrize("name", MODEL_FILES)
def test_the_header_names_the_hash_generating_command_for_the_serving_platform(name):
    text = (DEPLOY / name).read_text(encoding="utf-8")
    source = name.replace(".txt", ".in")
    command = f"uv pip compile deploy/{source} -o deploy/{name} {PLATFORM_FLAGS} --constraint deploy/requirements-serve.txt"
    assert command in text.splitlines()[1]
    assert command in (DEPLOY / source).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", MODEL_FILES)
def test_a_package_shared_with_the_serving_lock_is_pinned_at_the_serving_version(name):
    blocks = requirement_blocks(DEPLOY / name)
    shared = sorted(set(blocks) & set(SERVE))
    assert {"numpy", "onnxruntime"} <= set(shared)
    for package in shared:
        assert blocks[package][0] == SERVE[package][0], f"{name}: {package} {blocks[package][0]} != serving {SERVE[package][0]}"


def test_the_fp32_file_pins_the_libraries_the_bit_exactness_check_runs_on_to_the_serving_pins():
    blocks = requirement_blocks(DEPLOY / "requirements-model-fp32.txt")
    for package in FP32_SHARED:
        assert blocks[package][0] == SERVE[package][0]
    assert "onnx" in blocks and "onnx" not in SERVE         # onnx is a build tool: pinned here, never served


@pytest.mark.parametrize("name", MODEL_FILES)
def test_the_model_files_do_not_name_pymupdf(name):
    assert "pymupdf" not in (DEPLOY / name).read_text(encoding="utf-8").lower()


@pytest.mark.parametrize("text", [
    "FROM x AS model\nRUN pip install onnx\nFROM y AS web\n",
    'FROM x AS model\nRUN pip install "onnx==1.0"\nFROM y AS web\n',
    "FROM x AS model\nRUN pip install -r deploy/requirements-model.txt\nFROM y AS web\n",
])
def test_the_guard_flags_an_unhashed_install(text):
    commands = [cmd for step in model_stage(text) for cmd in pip_commands(step)]
    assert commands and not any(HASHED_INSTALL.search(cmd + " ") for cmd in commands)


def runtime_stage() -> list[str]:
    return instructions(DOCKERFILE.read_text(encoding="utf-8").split("\nFROM python:3.13-slim@", 2)[2])


def test_the_serving_install_builds_no_source_distribution():
    steps = [s for s in runtime_stage() if s.startswith("RUN uv pip install") and "requirements-serve.txt" in s]
    assert len(steps) == 1 and "--require-hashes" in steps[0] and "--no-build" in steps[0].split()


def test_the_project_install_takes_its_build_backend_from_the_hash_locked_build_file():
    runtime = runtime_stage()
    steps = [i for i, s in enumerate(runtime) if s.startswith("RUN uv pip install") and "--no-deps" in s]
    assert len(steps) == 1
    assert "--build-constraint deploy/requirements-build.txt" in runtime[steps[0]]
    copies = [i for i, s in enumerate(runtime) if s.startswith("COPY ") and "deploy/requirements-build.txt" in s]
    assert copies and min(copies) < steps[0]


def test_the_build_file_is_hashed_and_names_its_generating_command():
    blocks = requirement_blocks(DEPLOY / BUILD_FILE)
    assert "hatchling" in blocks
    for package, (_version, lines) in blocks.items():
        assert any(HASH.match(l) for l in lines), package
        assert not [l for l in lines if not l.lstrip().startswith(("--hash=", "#"))], package
    command = f"uv pip compile deploy/requirements-build.in -o deploy/{BUILD_FILE} {PLATFORM_FLAGS} --constraint deploy/requirements-serve.txt"
    assert command in (DEPLOY / BUILD_FILE).read_text(encoding="utf-8").splitlines()[1]
    for package in set(blocks) & set(SERVE):
        assert blocks[package][0] == SERVE[package][0]
    assert "hatchling" in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_a_corrupted_hash_line_is_caught(tmp_path):
    good = (DEPLOY / MODEL_FILES[0]).read_text(encoding="utf-8")
    bad = good.replace("    --hash=sha256:", "    --hashx=sha256:", 1)
    path = tmp_path / "bad.txt"
    path.write_text(bad, encoding="utf-8")
    blocks = requirement_blocks(path)
    assert any(not HASH.match(l) for _v, ls in blocks.values() for l in ls if not l.lstrip().startswith("#"))
