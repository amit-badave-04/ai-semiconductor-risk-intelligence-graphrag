"""Dockerfile: the embedder variant build argument. The default image must stay exactly today's (8-bit) image.

Docker is not available to these tests, so they pin the file itself: the default of ``EMBEDDER_VARIANT`` and everything
that resolves from it, the unchanged 8-bit build instruction, the pins of the fp32 branch against the serving pins, what
``KEEP_UNPATCHED`` ships; and, where a POSIX ``sh`` exists, they RUN the model stage's variant instruction with stubbed
``python`` / ``pip`` against a scratch directory, once per combination of the two build arguments, and check the layout
it leaves in ``/models``.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
Q8_DIR, FP32_DIR = "/models/qwen3-embedding-0.6b-q8", "/models/qwen3-embedding-0.6b-fp32"
Q8_MODEL_PATH = "/srv/models/qwen3-embedding-0.6b-q8/model_q8.onnx"
FP32_MODEL_PATH = "/srv/models/qwen3-embedding-0.6b-fp32/model_fp32.onnx"
Q8_BUILD = ('python scripts/build_onnx_embedder.py --out /models/qwen3-embedding-0.6b-q8 --no-verify '
            '&& python -c "import shutil; shutil.rmtree(\'/hf\', ignore_errors=True)"')


def instructions(text: str) -> list[str]:
    """Dockerfile instructions with continuation lines joined and comments dropped (``RUN a \\`` newline ``b`` -> one string)."""
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


def stages() -> tuple[list[str], list[str]]:
    """(model stage instructions, runtime stage instructions)."""
    steps = instructions(DOCKERFILE.read_text(encoding="utf-8"))
    froms = [i for i, step in enumerate(steps) if step.startswith("FROM ")]
    assert len(froms) == 2 and steps[froms[0]].endswith(" AS model")
    return steps[froms[0] + 1:froms[1]], steps[froms[1] + 1:]


MODEL, RUNTIME = stages()


def resolve(value: str, **args: str) -> str:
    for name, replacement in args.items():
        value = value.replace("${" + name + "}", replacement).replace("$" + name, replacement)
    return value


def variant_instruction() -> str:
    found = [step for step in MODEL if step.startswith("RUN ") and "EMBEDDER_VARIANT" in step]
    assert len(found) == 1
    return found[0]


# ---------------------------------------------------------------- the default stays the 8-bit image

def test_the_default_variant_is_q8_and_keep_unpatched_is_off():
    assert "ARG EMBEDDER_VARIANT=q8" in MODEL and "ARG KEEP_UNPATCHED=0" in MODEL
    assert "ARG EMBEDDER_VARIANT=q8" in RUNTIME


def test_the_8_bit_build_instruction_is_unchanged_and_comes_before_the_arguments():
    """Declared after it, the arguments cannot invalidate the cached 8-bit build when they change."""
    assert f"RUN {Q8_BUILD}" in MODEL
    assert MODEL.index(f"RUN {Q8_BUILD}") < MODEL.index("ARG EMBEDDER_VARIANT=q8")
    assert "COPY scripts/build_onnx_embedder.py scripts/" in MODEL


def test_the_model_path_of_the_default_image_is_today_s_and_matches_fly_toml():
    env = next(step for step in RUNTIME if step.startswith("ENV "))
    path = re.search(r"ONNX_MODEL_PATH=(\S+)", env).group(1)
    fly = re.search(r'ONNX_MODEL_PATH\s*=\s*"([^"]+)"', (ROOT / "fly.toml").read_text(encoding="utf-8")).group(1)

    assert resolve(path, EMBEDDER_VARIANT="q8") == Q8_MODEL_PATH == fly
    assert resolve(path, EMBEDDER_VARIANT="fp32") == FP32_MODEL_PATH       # a self-consistent fp32 image; fly.toml wins at run time


def test_nothing_else_in_the_runtime_stage_changes_with_the_variant():
    text = "\n".join(RUNTIME)

    assert "predequantize" not in text and "embedder_timing" not in text and "KEEP_UNPATCHED" not in text
    assert "embed_rss" not in text
    assert "COPY --from=model --chown=app:app /models /srv/models" in RUNTIME


def test_pymupdf_is_in_no_stage():
    assert "pymupdf" not in DOCKERFILE.read_text(encoding="utf-8").lower()


# ---------------------------------------------------------------- the fp32 branch

def test_the_variant_instruction_builds_from_the_8_bit_model_into_the_stable_fp32_path():
    run = variant_instruction()

    assert ("python scripts/predequantize_embedder.py --src /models/qwen3-embedding-0.6b-q8 "
            "--out /models/qwen3-embedding-0.6b-fp32") in run
    assert "COPY scripts/predequantize_embedder.py scripts/embedder_timing.py scripts/" in MODEL


def test_any_other_variant_fails_the_build():
    run = variant_instruction()

    assert re.search(r"\*\)\s*echo [^;]*EMBEDDER_VARIANT[^;]*>&2;\s*exit 1", run)


def serving_pins() -> dict[str, str]:
    text = (ROOT / "deploy" / "requirements-serve.txt").read_text(encoding="utf-8")
    return {name: re.search(rf"^{name}==(\S+)", text, re.M).group(1) for name in ("onnxruntime", "numpy", "tokenizers")}


def test_the_fp32_branch_pins_the_libraries_the_gate_runs_on_to_the_serving_pins():
    """The in-build bit-exactness check is against THIS onnxruntime: it must be the one the image serves with."""
    run = variant_instruction()
    install = re.search(r"pip install ([^;&]+)", run).group(1)

    for name, version in serving_pins().items():
        assert f'"{name}=={version}"' in install
    assert re.search(r'"onnx==\d+\.\d+\.\d+"', install)


# ---------------------------------------------------------------- KEEP_UNPATCHED

def test_keep_unpatched_keeps_the_8_bit_model_and_ships_the_timing_script_only_when_set():
    run = variant_instruction()

    assert 'if [ "$KEEP_UNPATCHED" != 1 ]; then rm -rf /models/qwen3-embedding-0.6b-q8; fi' in run
    assert 'if [ "$KEEP_UNPATCHED" = 1 ]; then cp scripts/embedder_timing.py /models/embedder_timing.py; fi' in run


# ---------------------------------------------------------------- the instruction itself, run against a scratch /models

def run_variant_instruction(tmp_path: Path, variant: str, keep: str):
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no POSIX sh on this machine")
    root = tmp_path.as_posix()
    (tmp_path / "build" / "scripts").mkdir(parents=True)
    (tmp_path / "build" / "scripts" / "embedder_timing.py").write_text("# timing\n", encoding="utf-8")
    (tmp_path / "models" / "qwen3-embedding-0.6b-q8").mkdir(parents=True)
    (tmp_path / "models" / "qwen3-embedding-0.6b-q8" / "model_q8.onnx").write_text("q8", encoding="utf-8")
    command = variant_instruction().removeprefix("RUN ").replace("/models", f"{root}/models")
    stubs = ('python() { echo "python $*" >> "$LOG"; while [ $# -gt 0 ]; do '
             'if [ "$1" = "--out" ]; then mkdir -p "$2"; : > "$2/model_fp32.onnx"; fi; shift; done; }\n'
             'pip() { echo "pip $*" >> "$LOG"; }\n')
    env = {**os.environ, "EMBEDDER_VARIANT": variant, "KEEP_UNPATCHED": keep, "LOG": f"{root}/calls.log"}
    done = subprocess.run([sh, "-c", stubs + command], cwd=tmp_path / "build", env=env, capture_output=True, text=True,
                          timeout=60)
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines() if (tmp_path / "calls.log").exists() else []
    layout = {p.relative_to(tmp_path / "models").as_posix() for p in (tmp_path / "models").rglob("*") if p.is_file()}
    return done, calls, layout


Q8_FILE, FP32_FILE, TIMING_FILE = ("qwen3-embedding-0.6b-q8/model_q8.onnx", "qwen3-embedding-0.6b-fp32/model_fp32.onnx",
                                   "embedder_timing.py")


@pytest.mark.parametrize("variant, keep, files, expects_build", [
    ("q8", "0", {Q8_FILE}, False),
    ("q8", "1", {Q8_FILE, TIMING_FILE}, False),
    ("fp32", "0", {FP32_FILE}, True),
    ("fp32", "1", {Q8_FILE, FP32_FILE, TIMING_FILE}, True),
], ids=["default-image", "q8-with-timing", "fp32", "fp32-for-the-timing-window"])
def test_the_variant_instruction_leaves_the_expected_models(tmp_path, variant, keep, files, expects_build):
    done, calls, layout = run_variant_instruction(tmp_path, variant, keep)

    assert done.returncode == 0, done.stderr
    assert layout == files
    built = [c for c in calls if "predequantize_embedder.py" in c]
    assert bool(built) == expects_build
    assert all(c.startswith(("pip", "python scripts/predequantize_embedder.py")) for c in calls)


def test_the_default_image_runs_no_command_at_all(tmp_path):
    done, calls, _ = run_variant_instruction(tmp_path, "q8", "0")

    assert done.returncode == 0 and calls == []


def test_an_unknown_variant_fails_and_leaves_the_8_bit_model_alone(tmp_path):
    done, calls, layout = run_variant_instruction(tmp_path, "int4", "0")

    assert done.returncode == 1 and "EMBEDDER_VARIANT" in done.stderr and "int4" in done.stderr
    assert calls == [] and layout == {Q8_FILE}


# ---------------------------------------------------------------- the memory probe (tools/probe/embed_rss.py)

PROBE_COPY = "COPY tools/probe/embed_rss.py scripts/"
PROBE_FILE = "embed_rss.py"


def probe_instruction() -> str:
    found = [step for step in MODEL if step.startswith("RUN ") and "embed_rss.py" in step]
    assert len(found) == 1
    return found[0]


def test_the_probe_is_copied_in_its_own_step_after_the_variant_build_and_only_the_model_stage_knows_it():
    """After the variant RUN, so editing the probe never invalidates the 2.3 GB dequantization layer (nor the 8-bit build)."""
    assert PROBE_COPY in MODEL
    assert MODEL.index(PROBE_COPY) > MODEL.index(variant_instruction())
    assert MODEL.index(probe_instruction()) == MODEL.index(PROBE_COPY) + 1
    assert "embed_rss" not in variant_instruction() and "embed_rss" not in "\n".join(RUNTIME)


def test_the_probe_is_shipped_only_when_keep_unpatched_is_set_and_to_the_path_the_w1_steps_use():
    run = probe_instruction()

    assert run == 'RUN if [ "$KEEP_UNPATCHED" = 1 ]; then cp scripts/embed_rss.py /models/embed_rss.py; fi'
    assert (ROOT / "tools" / "probe" / PROBE_FILE).is_file()
    assert "tools" not in (ROOT / ".dockerignore").read_text(encoding="utf-8").split()      # the build context has it


@pytest.mark.parametrize("keep, expected", [("0", set()), ("1", {PROBE_FILE})], ids=["default-image", "timing-window"])
def test_the_probe_step_leaves_the_probe_in_models_only_for_the_timing_window(tmp_path, keep, expected):
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no POSIX sh on this machine")
    root = tmp_path.as_posix()
    (tmp_path / "build" / "scripts").mkdir(parents=True)
    (tmp_path / "build" / "scripts" / PROBE_FILE).write_text("# probe\n", encoding="utf-8")
    (tmp_path / "models").mkdir()
    command = probe_instruction().removeprefix("RUN ").replace("/models", f"{root}/models")
    done = subprocess.run([sh, "-c", "set -eu; " + command], cwd=tmp_path / "build", env={**os.environ, "KEEP_UNPATCHED": keep},
                          capture_output=True, text=True, timeout=60)

    assert done.returncode == 0, done.stderr
    assert {p.name for p in (tmp_path / "models").iterdir()} == expected
