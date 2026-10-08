"""scripts/check_env_fly.py: the pre-deploy check of what the live machine will boot with (M5a closeout, V3 deploy blocker).

It builds ``Settings`` from the ``[env]`` table of ``fly.toml`` plus the secrets of ``.env.fly`` that ``push_fly_secrets``
would push, as the live app (``FLY_APP_NAME`` from ``fly.toml``), through the production validators, and prints SETTING
NAMES with PASS or FAIL and the validator's reason text: never a value. Every fixture here is a fake file under
``tmp_path`` carrying distinctive sentinel values, and every run asserts that none of them (nor a value of ``fly.toml``)
reaches stdout or stderr. The real ``.env.fly`` is never opened.
"""

import importlib.util
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from semigraph import config

ROOT = Path(__file__).resolve().parent.parent
FLY_TOML = ROOT / "fly.toml"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check_env_fly = _load("check_env_fly", ROOT / "scripts" / "check_env_fly.py")
push_fly_secrets = _load("push_fly_secrets_for_the_check", ROOT / "scripts" / "push_fly_secrets.py")

# Distinctive, odd-numbered fakes: "true", "150" and "fly-client-ip" legitimately appear in reason text, these never do.
SECRETS = {
    "IP_HASH_PEPPER": "chk-pepper-ZETA-137-" + "x" * 30,                  # gitleaks:allow
    "IP_HASH_VERSION": "7",
    "ADMIN_TOKEN": "chk-admin-ETA-137-" + "y" * 30,                       # gitleaks:allow
    "TURNSTILE_SECRET_KEY": "chk-turnstile-THETA-137-" + "z" * 20,        # gitleaks:allow
    "TURNSTILE_SITE_KEY": "chk-sitekey-KAPPA-137",                        # gitleaks:allow
    "NEO4J_PASSWORD": "chk-neo4j-IOTA-137-pw",                            # gitleaks:allow
    "ANTHROPIC_API_KEY": "sk-ant-chk-MU-137-fake",                        # gitleaks:allow
    "OPENAI_API_KEY": "sk-chk-NU-137-fake",                               # gitleaks:allow
    "SEC_USER_AGENT": "Chk Person chk-XI-137@example.invalid",
}
SENTINELS = [v for v in SECRETS.values() if len(v) > 8]
FLY = tomllib.loads(FLY_TOML.read_text(encoding="utf-8"))
FLY_VALUES = [str(v) for v in FLY["env"].values() if "/" in str(v)]       # model names and the ONNX path: never in a reason


@pytest.fixture(autouse=True)
def hermetic(monkeypatch, tmp_path):
    """The check must not read the shell or a ``.env`` in the working directory: poison both, then expect a clean run."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("MAX_QUERIES_PER_DAY=999\nENVIRONMENT=development\nADMIN_TOKEN=bad\n", encoding="utf-8")
    for name, value in {"ENVIRONMENT": "development", "ADMIN_TOKEN": "short", "FLY_APP_NAME": "elsewhere",
                        "RATE_LIMIT_QUESTIONS": "0", "TURNSTILE_REQUIRED": "false"}.items():
        monkeypatch.setenv(name, value)


def env_file(tmp_path: Path, secrets: dict | None = None, *, extra: str = "", bom: bool = False, crlf: bool = False,
             drop: tuple[str, ...] = ()) -> Path:
    values = {k: v for k, v in (SECRETS if secrets is None else secrets).items() if k not in drop}
    text = "# fixture\n" + "".join(f"{k}={v}\n" for k, v in values.items()) + extra
    if crlf:
        text = text.replace("\n", "\r\n")
    path = tmp_path / ".env.fly"
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return path


def run(env: Path, fly_toml: Path = FLY_TOML, capsys=None) -> tuple[int, str]:
    code = check_env_fly.main(["--env", str(env), "--fly-toml", str(fly_toml)])
    out = capsys.readouterr()
    assert out.err == ""
    return code, out.out


def lines_of(out: str, status: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith(status + " ")]


def names_of(out: str, status: str) -> list[str]:
    return [line.split()[1].rstrip(":") for line in lines_of(out, status)]


def assert_no_value_printed(out: str, extra: tuple[str, ...] = ()) -> None:
    assert [v for v in (*SENTINELS, *FLY_VALUES, *extra) if v in out] == []


# ---------------------------------------------------------------- today's shape passes

def test_the_live_fly_toml_and_a_complete_set_of_secrets_pass_and_print_only_names(tmp_path, capsys):
    code, out = run(env_file(tmp_path), capsys=capsys)

    assert code == 0 and "RESULT: PASS" in out
    assert names_of(out, "PASS") == [n for n in config.CHECKED_SETTINGS]
    assert lines_of(out, "FAIL") == []
    assert_no_value_printed(out)


def test_a_bom_and_crlf_in_the_file_still_parse_and_the_bom_is_a_warning(tmp_path, capsys):
    """A BOM at the start of .env.fly has bitten this project: the first key read as ``\\ufeffNAME``."""
    code, out = run(env_file(tmp_path, bom=True, crlf=True), capsys=capsys)

    assert code == 0 and names_of(out, "FAIL") == []
    warned = " ".join(lines_of(out, "WARN"))
    assert "BOM" in warned and "push_fly_secrets" in warned
    assert_no_value_printed(out)


def test_the_parser_reads_exactly_what_push_fly_secrets_reads(tmp_path):
    path = tmp_path / "parity.env"
    path.write_text('# c\n\nA=1\n B = "two words" \nC=\'q\'\nnot a line\nD=x=y\nE=\n', encoding="utf-8")

    assert check_env_fly.read_env_file(path)[0] == push_fly_secrets.parse_env(path)


# ---------------------------------------------------------------- failures name the setting and never the value

def test_a_missing_pepper_fails_naming_it(tmp_path, capsys):
    code, out = run(env_file(tmp_path, drop=("IP_HASH_PEPPER",)), capsys=capsys)

    assert code == 1 and "RESULT: FAIL" in out
    assert names_of(out, "FAIL") == ["IP_HASH_PEPPER"]
    assert "at least 32 bytes" in lines_of(out, "FAIL")[0]
    assert_no_value_printed(out)


def test_a_short_admin_token_fails_and_is_never_echoed(tmp_path, capsys):
    code, out = run(env_file(tmp_path, {**SECRETS, "ADMIN_TOKEN": "short-sentinel-137"}), capsys=capsys)

    assert code == 1 and names_of(out, "FAIL") == ["ADMIN_TOKEN"]
    assert_no_value_printed(out, ("short-sentinel-137",))


def test_a_raised_cap_in_the_secrets_fails_naming_only_that_setting(tmp_path, capsys):
    code, out = run(env_file(tmp_path, extra="MAX_QUERIES_PER_DAY=999\n"), capsys=capsys)

    assert code == 1 and names_of(out, "FAIL") == ["MAX_QUERIES_PER_DAY"]
    assert "999" not in out


def test_a_value_of_the_wrong_type_fails_by_name_without_the_value_and_says_the_cross_checks_did_not_run(tmp_path, capsys):
    code, out = run(env_file(tmp_path, extra="MAX_QUERIES_PER_DAY=sentinel-not-a-number-137\n"), capsys=capsys)

    assert code == 1 and names_of(out, "FAIL") == ["MAX_QUERIES_PER_DAY"]
    assert "sentinel-not-a-number-137" not in out
    assert any("did not run" in line for line in lines_of(out, "WARN"))


def test_a_problem_is_attributed_to_its_own_setting_not_to_one_that_contains_its_name(tmp_path, capsys):
    """RATE_LIMIT_QUESTIONS is a substring of FREE_RATE_LIMIT_QUESTIONS."""
    zeroed = tmp_path / "fly.toml"
    zeroed.write_text(FLY_TOML.read_text(encoding="utf-8").replace("[env]\n", '[env]\n  RATE_LIMIT_QUESTIONS = "0"\n'),
                      encoding="utf-8")

    code, out = run(env_file(tmp_path), zeroed, capsys=capsys)

    assert code == 1 and names_of(out, "FAIL") == ["RATE_LIMIT_QUESTIONS"]
    assert "FREE_RATE_LIMIT_QUESTIONS" in names_of(out, "PASS")


def test_an_environment_that_is_not_production_fails_as_it_would_on_the_live_app(tmp_path, capsys):
    """FLY_APP_NAME comes from fly.toml's app: a [env] without ENVIRONMENT would boot the live app as development."""
    no_environment = tmp_path / "fly.toml"
    no_environment.write_text(FLY_TOML.read_text(encoding="utf-8").replace('ENVIRONMENT = "production"', "# none"),
                              encoding="utf-8")

    code, out = run(env_file(tmp_path), no_environment, capsys=capsys)

    assert code == 1 and names_of(out, "FAIL") == ["ENVIRONMENT"]


def test_every_problem_is_listed_at_once(tmp_path, capsys):
    code, out = run(env_file(tmp_path, {**SECRETS, "ADMIN_TOKEN": "short-sentinel-137"}, drop=("IP_HASH_PEPPER",),
                             extra="MAX_QUERIES_PER_DAY=999\n"), capsys=capsys)

    assert code == 1 and sorted(names_of(out, "FAIL")) == ["ADMIN_TOKEN", "IP_HASH_PEPPER", "MAX_QUERIES_PER_DAY"]


# ---------------------------------------------------------------- what is merged, and what is warned about

def test_a_secret_that_is_also_in_fly_toml_env_is_a_warning_naming_it(tmp_path, capsys):
    code, out = run(env_file(tmp_path, extra="ESCALATION_MODEL=anthropic/claude-sonnet-5\n"), capsys=capsys)

    assert code == 0
    warning = next(line for line in lines_of(out, "WARN") if "ESCALATION_MODEL" in line)
    assert "[env]" in warning and "secret" in warning
    assert "anthropic/claude-sonnet-5" not in out


def test_a_key_the_default_push_would_not_send_never_reaches_validation_and_is_listed(tmp_path, capsys):
    """CLIENT_IP_HEADER is in fly.toml [env] already; TURNSTILE_REQUIRED is not a pushed key: a value for either in
    .env.fly would not reach the machine, so it cannot fix (or break) the boot, and the check says so by name."""
    code, out = run(env_file(tmp_path, extra="CLIENT_IP_HEADER=x-forwarded-for\nTURNSTILE_REQUIRED=false\n"),
                    capsys=capsys)

    assert code == 0 and names_of(out, "FAIL") == []
    warning = next(line for line in lines_of(out, "WARN") if "not pushed" in line)
    assert "CLIENT_IP_HEADER" in warning and "TURNSTILE_REQUIRED" in warning
    assert "x-forwarded-for" not in out


def test_the_secrets_missing_from_the_file_are_listed_by_name(tmp_path, capsys):
    code, out = run(env_file(tmp_path, drop=("OPENAI_API_KEY", "SEC_USER_AGENT")), capsys=capsys)

    assert code == 0
    warning = next(line for line in lines_of(out, "WARN") if "missing from" in line)
    for name in ("OPENAI_API_KEY", "SEC_USER_AGENT"):
        assert name in warning
    assert "IP_HASH_PEPPER" not in warning and "ADMIN_TOKEN" not in warning
    expected_missing = [k for k in push_fly_secrets.FLY_KEYS["semigraph"] if k not in SECRETS or k in
                        ("OPENAI_API_KEY", "SEC_USER_AGENT")]
    assert all(name in warning for name in expected_missing)


def test_an_empty_value_counts_as_missing_as_it_does_for_the_push(tmp_path, capsys):
    """push_fly_secrets skips an empty value: it never reaches the machine."""
    code, out = run(env_file(tmp_path, extra="OPENAI_API_KEY=\n"), capsys=capsys)     # the later, empty line wins

    assert code == 0
    assert "OPENAI_API_KEY" in next(line for line in lines_of(out, "WARN") if "missing from" in line)


def test_the_shell_and_a_dotenv_in_the_working_directory_are_ignored(tmp_path, capsys):
    """The autouse fixture exports a development ENVIRONMENT, a short ADMIN_TOKEN, a foreign FLY_APP_NAME, a zeroed window
    and TURNSTILE_REQUIRED=false, and writes a poisoned .env: none of it may reach the check."""
    code, out = run(env_file(tmp_path), capsys=capsys)

    assert code == 0 and lines_of(out, "FAIL") == []


# ---------------------------------------------------------------- the model base variables: files only, never the shell

MODEL_BASES = ("OPENAI_BASE_URL", "OPENAI_API_BASE", "ANTHROPIC_API_BASE", "ANTHROPIC_BASE_URL")


def base_sentinel(name: str) -> str:
    return f"https://chk-gateway-{name.lower().replace('_', '-')}-LAMBDA-137.invalid/v1"


def test_the_check_covers_exactly_the_model_base_variables_production_refuses():
    assert check_env_fly.MODEL_BASE_VARIABLES == MODEL_BASES
    assert set(MODEL_BASES) <= set(config.CHECKED_SETTINGS)


def test_a_base_url_exported_in_the_shell_is_not_seen_and_is_still_in_the_shell_afterwards(tmp_path, capsys, monkeypatch):
    """Claude Code's own environment exports ``ANTHROPIC_BASE_URL``, and a gateway wrapper may export the others: the check
    judges ``.env.fly`` and ``fly.toml [env]``, so none of them may fail it, and it must put the shell back as it found it."""
    for name in MODEL_BASES:
        monkeypatch.setenv(name, base_sentinel(name))

    code, out = run(env_file(tmp_path), capsys=capsys)

    assert code == 0 and lines_of(out, "FAIL") == [] and "RESULT: PASS" in out
    assert names_of(out, "PASS") == [n for n in config.CHECKED_SETTINGS]
    assert_no_value_printed(out, tuple(base_sentinel(name) for name in MODEL_BASES))
    assert {name: os.environ.get(name) for name in MODEL_BASES} == {name: base_sentinel(name) for name in MODEL_BASES}


@pytest.mark.parametrize("name", MODEL_BASES)
def test_the_shell_is_put_back_even_when_the_check_fails(name, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv(name, base_sentinel(name))
    monkeypatch.setenv("ENVIRONMENT", "kept-in-the-shell")

    code, _ = run(env_file(tmp_path, drop=("IP_HASH_PEPPER",)), capsys=capsys)

    assert code == 1
    assert os.environ[name] == base_sentinel(name) and os.environ["ENVIRONMENT"] == "kept-in-the-shell"


@pytest.mark.parametrize("name", MODEL_BASES)
def test_a_base_url_in_fly_toml_env_fails_naming_it_and_never_prints_its_value(name, tmp_path, capsys):
    with_base = tmp_path / "fly.toml"
    with_base.write_text(FLY_TOML.read_text(encoding="utf-8").replace("[env]\n", f'[env]\n  {name} = "{base_sentinel(name)}"\n'),
                         encoding="utf-8")

    code, out = run(env_file(tmp_path), with_base, capsys=capsys)

    assert code == 1 and names_of(out, "FAIL") == [name]
    assert "must be empty in production" in lines_of(out, "FAIL")[0]
    assert_no_value_printed(out, (base_sentinel(name),))


@pytest.mark.parametrize("name", MODEL_BASES)
def test_a_base_url_in_env_fly_fails_naming_it_though_the_default_push_would_not_send_it(name, tmp_path, capsys):
    """The default push sends a fixed list of keys, and these are not on it; but ``push_fly_secrets --only`` can send any key
    of the file, and a green check followed by that push would boot a machine that refuses to start. So these four are judged
    wherever they appear, and a value that would divert the live model calls is a FAIL, not the 'not pushed' warning."""
    assert name not in push_fly_secrets.FLY_KEYS["semigraph"]

    code, out = run(env_file(tmp_path, extra=f"{name}={base_sentinel(name)}\n"), capsys=capsys)

    assert code == 1 and names_of(out, "FAIL") == [name] and "RESULT: FAIL" in out
    assert not any(name in line for line in lines_of(out, "WARN"))           # reported once, as a failure
    assert_no_value_printed(out, (base_sentinel(name),))


@pytest.mark.parametrize("name", MODEL_BASES)
def test_an_empty_base_url_in_env_fly_is_not_a_base(name, tmp_path, capsys):
    """An exported-but-empty variable is falsy for LiteLLM's ``or`` chain, and the push skips an empty value."""
    code, out = run(env_file(tmp_path, extra=f"{name}=\n"), capsys=capsys)

    assert code == 0 and lines_of(out, "FAIL") == []


# ---------------------------------------------------------------- never a value, whatever goes wrong

def test_a_missing_file_names_the_file_only(tmp_path, capsys):
    code, out = run(tmp_path / "nope.env", capsys=capsys)

    assert code == 1 and "nope.env" in out and "RESULT: FAIL" in out and str(tmp_path) not in out


def test_an_unexpected_failure_prints_the_exception_type_only(tmp_path, capsys):
    broken = tmp_path / "fly.toml"
    broken.write_text('[env]\nSECRET_LOOKING = "chk-toml-OMICRON-137\n', encoding="utf-8")

    code, out = run(env_file(tmp_path), broken, capsys=capsys)

    assert code == 1 and "TOMLDecodeError" in out and "chk-toml-OMICRON-137" not in out


def test_the_script_runs_as_a_program_and_prints_nothing_on_stderr(tmp_path):
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE") if k in os.environ}
    env.update(PYTHONPATH=str(ROOT / "src"), PYTHONUTF8="1")
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_env_fly.py"), "--env",
                           str(env_file(tmp_path)), "--fly-toml", str(FLY_TOML)], capture_output=True, text=True,
                          cwd=tmp_path, env=env, timeout=120)

    assert done.returncode == 0 and done.stderr == "" and "RESULT: PASS" in done.stdout
    assert_no_value_printed(done.stdout)


def test_the_defaults_are_the_repo_files_and_nothing_is_read_to_learn_them():
    args = check_env_fly.parse_args([])
    assert Path(args.env).name == ".env.fly" and Path(args.fly_toml).name == "fly.toml"
