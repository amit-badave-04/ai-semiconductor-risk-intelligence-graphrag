"""Push secrets from the local .env.fly to Fly.io without values touching shell history.

    python -m scripts.push_fly_secrets [--env .env.fly] [--app semigraph]
                                       [--only KEY1,KEY2] [--dry-run] [--stage] [--flyctl PATH]

Reads the env file, filters to the known secret keys (never pushes local-only
settings), and calls `flyctl secrets import` once with all pairs on STDIN — a
single app restart. Values go through stdin (never argv, so they do not appear
in the local process list), never through a shell, and only key NAMES are printed.
The Neo4j app gets its single secret (NEO4J_AUTH) with --app semigraph-neo4j.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Keys that belong on each app. Anything else in .env.fly stays local.
FLY_KEYS = {
    "semigraph": [
        "ANTHROPIC_API_KEY", "LLM_MODEL", "CRITIC_MODEL",
        "NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD",
        "ADMIN_TOKEN", "APP_BASE_URL", "MAX_QUERIES_PER_DAY",
        "TURNSTILE_SITE_KEY", "TURNSTILE_SECRET_KEY",
        "EMBEDDING_API_BASE", "EMBEDDING_API_KEY", "EMBEDDING_API_MODEL",
    ],
    "semigraph-neo4j": ["NEO4J_AUTH"],
}


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.fly")
    parser.add_argument("--app", default="semigraph", choices=sorted(FLY_KEYS))
    parser.add_argument("--only", help="comma-separated key names (subset of the env file)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stage", action="store_true", help="stage without restarting machines")
    parser.add_argument("--flyctl", default=None, help="path to flyctl if not on PATH")
    args = parser.parse_args()

    env_path = Path(args.env)
    if not env_path.exists():
        sys.exit(f"ERROR: {env_path} not found")
    env = parse_env(env_path)

    wanted = [k.strip() for k in args.only.split(",")] if args.only else FLY_KEYS[args.app]
    missing = [k for k in wanted if not env.get(k)]
    pairs = {k: env[k] for k in wanted if env.get(k)}
    if missing:
        print(f"skipping (empty or absent in {env_path.name}): {', '.join(missing)}")
    if not pairs:
        sys.exit("ERROR: nothing to push")

    print(f"pushing {len(pairs)} secrets to {args.app}: {', '.join(pairs)}")
    if args.dry_run:
        print("dry run — not calling flyctl")
        return

    flyctl = args.flyctl or shutil.which("flyctl") or shutil.which("fly")
    if not flyctl:
        sys.exit("ERROR: flyctl not on PATH — pass --flyctl <full path to flyctl.exe>")
    cmd = [flyctl, "secrets", "import", "-a", args.app] + (["--stage"] if args.stage else [])
    payload = "".join(f"{key}={value}\n" for key, value in pairs.items())
    result = subprocess.run(cmd, input=payload, text=True, cwd=env_path.resolve().parent)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
