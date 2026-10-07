"""Pre-deploy check: would the live machine boot with what it will be given?

    uv run python -m scripts.check_env_fly [--env .env.fly] [--fly-toml fly.toml]

Builds ``semigraph.config.Settings`` the way the live machine does and prints, for every setting the production validators
cover, ``PASS`` or ``FAIL`` with the validator's reason. Only setting NAMES and the validators' own fixed texts are printed:
no value is ever printed, logged or written (pydantic's ``input_value`` is switched off, the errors are read without their
input, and an unexpected failure prints its exception type and nothing else). Exit code 0 when every validator passes, 1
otherwise.

What the machine sees, and what this reads:

* ``fly.toml [env]``, and the app name of ``fly.toml`` as ``FLY_APP_NAME`` (Fly sets it on every machine; ``ENVIRONMENT``
  comes from ``[env]`` like it does there, so a missing one fails the app-name tie instead of being assumed);
* the secrets of ``.env.fly`` that ``scripts/push_fly_secrets.py`` pushes by default (its ``FLY_KEYS["semigraph"]``, and
  only non-empty values, as the push does). A key of the file outside that list never reaches the machine unless it is
  pushed with ``--only``, so it is not validated and is reported by name;
* nothing else: not the shell's variables and not a ``.env`` in the working directory.

Assumption, not verified (Fly's documentation of secrets and ``[env]`` does not say which wins when both define a name):
a secret is taken to override ``[env]``. A name in both is a warning; keep it in one place.
"""

import argparse
import codecs
import importlib.util
import re
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from semigraph.config import CHECKED_SETTINGS, Settings

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
APP_SECRETS = "semigraph"           # the key of FLY_KEYS the live API app takes its secrets from
_NAME = re.compile(r"^([A-Z][A-Z0-9_]*) ")
# The validators join their problems with "; " (a hint inside one problem may contain "; print(", so only a split before
# the next SETTING NAME counts).
_BETWEEN_PROBLEMS = re.compile(r"; (?=[A-Z][A-Z0-9_]{2,} )")


class LiveSettings(Settings):
    """``Settings`` from the keyword arguments ONLY: the environment variables, a ``.env`` and the secrets directory of
    this process are not sources, so the check cannot be satisfied or broken by what the developer's shell holds."""

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings,
                                   file_secret_settings):
        return (init_settings,)


@dataclass(frozen=True)
class Report:
    """What the check found. Names and fixed reason texts only: nothing here can hold a value."""

    failures: dict[str, str] = field(default_factory=dict)       # setting name -> the validator's reason
    warnings: list[str] = field(default_factory=list)
    problem: str = ""                                            # the check itself could not run

    @property
    def ok(self) -> bool:
        return not self.failures and not self.problem


def _push_module():
    spec = importlib.util.spec_from_file_location("push_fly_secrets", HERE / "push_fly_secrets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_env_file(path: Path) -> tuple[dict[str, str], bool]:
    """``(values, had_a_bom)``. Parsed exactly as ``push_fly_secrets.parse_env`` does, except that it decodes
    ``utf-8-sig`` (a BOM at the start of the file made the first key unreadable to the push) and that CRLF is fine."""
    raw = path.read_bytes()
    values: dict[str, str] = {}
    for line in raw.decode("utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values, raw.startswith(codecs.BOM_UTF8)


def explain(error: ValidationError) -> dict[str, str]:
    """``{SETTING: reason}`` from a refusal, without its input. A field error names its field; a model-level one carries
    every problem in one text, each beginning with the setting it is about."""
    reasons: dict[str, str] = {}
    for item in error.errors(include_input=False, include_url=False, include_context=False):
        message = item["msg"].removeprefix("Value error, ")
        if item["loc"]:
            reasons[str(item["loc"][0]).upper()] = message
            continue
        for part in _BETWEEN_PROBLEMS.split(message):
            named = _NAME.match(part)
            reasons[named.group(1) if named else "SETTINGS"] = part
    return reasons


def _warnings(env_table: dict, file_values: dict[str, str], pushed: list[str], had_bom: bool) -> list[str]:
    warnings = []
    if had_bom:
        warnings.append("WARN  .env.fly starts with a UTF-8 BOM: read correctly here and by scripts/push_fly_secrets.py "
                        "(both decode utf-8-sig), but other tools may read the first key as \\ufeffNAME; save the file "
                        "without a BOM")
    overlap = sorted(set(env_table) & {k for k in pushed if file_values.get(k)})
    if overlap:
        warnings.append("WARN  also set as a secret in .env.fly and in fly.toml [env] (the secret is assumed to win; "
                        f"keep each in one place): {', '.join(overlap)}")
    unpushed = sorted(set(file_values) - set(pushed))
    if unpushed:
        warnings.append("WARN  in .env.fly but not pushed by scripts/push_fly_secrets.py by default (they reach the "
                        f"machine only with --only, so they are not checked): {', '.join(unpushed)}")
    missing = [k for k in pushed if not file_values.get(k)]
    if missing:
        warnings.append(f"WARN  missing from .env.fly (empty or absent; a secret that exists only on Fly is not seen): "
                        f"{', '.join(missing)}")
    return warnings


def check(env_path: Path, fly_toml_path: Path) -> Report:
    """Build the live ``Settings`` from the two files. Raises FileNotFoundError, tomllib.TOMLDecodeError or KeyError for a
    file that cannot be read; ``main`` turns those into a name-only report."""
    fly = tomllib.loads(fly_toml_path.read_text(encoding="utf-8"))
    env_table = {key: str(value) for key, value in fly.get("env", {}).items()}
    file_values, had_bom = read_env_file(env_path)
    pushed = list(_push_module().FLY_KEYS[APP_SECRETS])
    secrets = {key: file_values[key] for key in pushed if file_values.get(key)}
    values = {key.lower(): value for key, value in {**env_table, **secrets}.items()}
    warnings = _warnings(env_table, file_values, pushed, had_bom)
    try:
        LiveSettings(**values, fly_app_name=fly["app"])
    except ValidationError as error:
        if any(item["loc"] for item in error.errors(include_input=False, include_url=False, include_context=False)):
            warnings.append("WARN  a setting could not be read as its type, so the checks across settings did not run: "
                            "fix it and run this again")
        return Report(failures=explain(error), warnings=warnings)
    return Report(warnings=warnings)


def render(report: Report) -> str:
    lines = ["check_env_fly: fly.toml [env] + the secrets of .env.fly, as the live app (names only, no value is printed)"]
    if report.problem:
        lines.append(f"FAIL  {report.problem}")
    else:
        extra = [name for name in report.failures if name not in CHECKED_SETTINGS]
        for name in (*CHECKED_SETTINGS, *extra):
            lines.append(f"FAIL  {name}  {report.failures[name]}" if name in report.failures else f"PASS  {name}")
    lines += report.warnings
    lines.append("RESULT: PASS" if report.ok else f"RESULT: FAIL ({len(report.failures) or 1})")
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="check_env_fly", description=__doc__.splitlines()[0])
    parser.add_argument("--env", default=str(ROOT / ".env.fly"), help="the secrets file (default: .env.fly)")
    parser.add_argument("--fly-toml", default=str(ROOT / "fly.toml"), help="the Fly config (default: fly.toml)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = check(Path(args.env), Path(args.fly_toml))
    except FileNotFoundError as error:
        report = Report(problem=f"{Path(error.filename).name}  not found" if error.filename else "a file was not found")
    except Exception as error:  # noqa: BLE001 - only the type is printed: a message could carry a value
        report = Report(problem=f"check_env_fly could not run ({type(error).__name__})")
    print(render(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
