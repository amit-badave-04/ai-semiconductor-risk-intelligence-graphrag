#!/bin/sh
# Containment check of the production image (docs/security/accepted-risks.md R5-7 pre-I4 check, R5-3 gate;
# docs/specs/m5b-frontend.md Acceptance Warning 4 and R13).
#
# Runs INSIDE the built image, as the serving user, while the API server is up:
#   docker exec -u app -e IMAGE_CHECK_MODE=ci <container> sh /srv/checks/image_check.sh
# The container was started with fake secrets (a canary value per secret name) so that "no secret is readable" is a measurement and
# not a hope. Prints one PASS, SKIP or FAIL line per check and exits non-zero when any check FAILs or never reported. A SKIP counts as a FAIL in ci mode.
# POSIX sh only (the image is python:3.13-slim: dash, coreutils, findutils, grep, mawk). It writes no file: a file here would show
# up in the /tmp comparison below.

EXPECTED='tools_present not_root api_runs_as_app worker_fork_refused canary_env_present api_pid_found api_proc_environ_unreadable api_proc_root_owned canary_not_in_files canary_not_in_api_proc canary_not_in_readable_proc_environ no_env_file worker_env_scrubbed worker_no_new_privs worker_nproc_zero worker_proc_root_owned worker_parse_ok worker_leaves_no_tmp worker_cmd_flags no_setuid nothing_writable dist_root_owned dist_readable dist_path_default srv_root_owned python_env_flags no_node workspace_store_none'
SECRET_NAMES="NEO4J_PASSWORD ANTHROPIC_API_KEY OPENAI_API_KEY TURNSTILE_SECRET_KEY ADMIN_TOKEN EMBEDDING_API_KEY ORIGIN_AUTH_SECRET IP_HASH_PEPPER LANGFUSE_SECRET_KEY LANGFUSE_HASH_SALT"
# IMAGE_CHECK_MODE=deployed: run on the live machine (RUNBOOK), where the secrets are real and there are no canaries. The canary
# checks are then reported as SKIP (counted apart, never as PASS), everything else is the same.
mode=${IMAGE_CHECK_MODE:-ci}
skipped="skipped: no canaries in deployed mode"
# The prefix of every canary value, built from two halves so that this file (it lives under /srv) never matches its own search.
pat="CANARY""_"

# harness begin
fails=0
skips=0
reported=" "
report() {
    # report <name> <PASS|SKIP|FAIL> <detail>: only the exact word PASS passes; SKIP is a FAIL in ci mode
    case "$2" in
        PASS) printf 'PASS %s %s\n' "$1" "$3" ;;
        SKIP)
            printf 'SKIP %s %s\n' "$1" "$3"
            if [ "$mode" = ci ]; then fails=$((fails + 1)); else skips=$((skips + 1)); fi
            ;;
        *) printf 'FAIL %s %s\n' "$1" "$3"; fails=$((fails + 1)) ;;
    esac
    reported="$reported$1 "
}
finish() {
    for n in $EXPECTED; do
        case "$reported" in
            *" $n "*) ;;
            *) printf 'FAIL %s never reported\n' "$n"; fails=$((fails + 1)) ;;
        esac
    done
    if [ "$fails" -eq 0 ]; then
        if [ "$skips" -eq 0 ]; then echo "image check: all checks PASS"; else echo "image check: all checks PASS, $skips skipped (deployed mode)"; fi
        exit 0
    fi
    echo "image check: $fails FAIL"
    exit 1
}
# harness end

# --- the tools this script relies on: a missing one would make the checks built on it pass on empty output
missing=""
for t in find grep stat awk tr printenv dirname id head cat; do
    command -v "$t" >/dev/null 2>&1 || missing="$missing $t"
done
if [ -z "$missing" ]; then report tools_present PASS "find grep stat awk tr printenv dirname id head cat"; else report tools_present FAIL "missing:$missing"; fi

# --- who we are
uid=$(id -u)
if [ "$uid" -ne 0 ]; then report not_root PASS "uid $uid"; else report not_root FAIL "running as root"; fi

# the canaries must reach this session (docker exec inherits the container environment), or every canary check is vacuous
if [ "$mode" = deployed ]; then
    report canary_env_present SKIP "$skipped"
else
    missing=""
    for n in $SECRET_NAMES; do
        v=$(printenv "$n")
        case "$v" in "$pat$n") ;; *) missing="$missing $n" ;; esac
    done
    # positive control: the search used below must find a canary where one is known to be (this shell's own environment)
    grep -qaF -e "$pat" /proc/$$/environ 2>/dev/null || missing="$missing (positive control: grep found no canary in its own environment)"
    if [ -z "$missing" ]; then report canary_env_present PASS "all canary variables set"; else report canary_env_present FAIL "not set:$missing"; fi
fi

# --- the API server process
api_pid=""
for c in /proc/[0-9]*/cmdline; do
    p=${c#/proc/}; p=${p%/cmdline}
    args=$(tr '\0' ' ' < "$c" 2>/dev/null)
    case "$args" in
        *"-m semigraph.serve.drain"*) api_pid=$p; break ;;
    esac
done
if [ -n "$api_pid" ]; then
    report api_pid_found PASS "pid $api_pid"
    if cat "/proc/$api_pid/environ" >/dev/null 2>&1; then
        report api_proc_environ_unreadable FAIL "the serving user can read /proc/$api_pid/environ"
    else
        report api_proc_environ_unreadable PASS "read refused"
    fi
    owner=$(stat -c %u "/proc/$api_pid" 2>/dev/null)
    if [ "$owner" = "0" ]; then report api_proc_root_owned PASS "/proc/$api_pid is owned by uid 0 (non-dumpable)"; else report api_proc_root_owned FAIL "/proc/$api_pid owner uid '$owner'"; fi
    # the server must run as the serving user: /proc/<pid>/status is world-readable (the entry is root-owned but not hidden)
    app_uid=$(id -u app 2>/dev/null)
    uids=$(awk '/^Uid:/ {print $2 " " $3}' "/proc/$api_pid/status" 2>/dev/null)
    if [ -n "$app_uid" ] && [ "$app_uid" != 0 ] && [ "$uids" = "$app_uid $app_uid" ]; then report api_runs_as_app PASS "real and effective uid $app_uid"; else report api_runs_as_app FAIL "server uids '$uids', app uid '$app_uid'"; fi
    if [ "$mode" = deployed ]; then
        report canary_not_in_api_proc SKIP "$skipped"
    else
        hit=""
        for f in environ cmdline maps; do
            grep -qaF -e "$pat" "/proc/$api_pid/$f" 2>/dev/null
            if [ $? -eq 0 ]; then hit="$hit $f"; fi
        done
        if [ -z "$hit" ]; then report canary_not_in_api_proc PASS "none in environ, cmdline, maps (where readable)"; else report canary_not_in_api_proc FAIL "canary found in:$hit"; fi
    fi
else
    report api_pid_found FAIL "no server process (it exits about 90 s after boot without a database: rerun the job)"
    report api_proc_environ_unreadable FAIL "no server pid"
    report api_proc_root_owned FAIL "no server pid"
    report api_runs_as_app FAIL "no server pid"
    report canary_not_in_api_proc FAIL "no server pid"
fi

# --- no canary anywhere the serving user can read (file names only are printed, never contents)
if [ "$mode" = deployed ]; then
    report canary_not_in_files SKIP "$skipped"
else
    # /srv is searched through its entries other than /srv/models (the model weights, large and not text); a directory called
    # models anywhere else is searched like any other
    srv_entries=$(find /srv -mindepth 1 -maxdepth 1 ! -path /srv/models 2>/dev/null)
    found=$(grep -rlaF -e "$pat" /tmp /var/tmp /dev/shm /run $srv_entries /opt /nonexistent /etc /var/log "$PWD" 2>/dev/null | head -n 5)
    if [ -z "$found" ]; then report canary_not_in_files PASS "none under /tmp /srv /opt /etc /run /var/log or the working directory"; else report canary_not_in_files FAIL "found in: $found"; fi
fi

# every other process whose environment this user can read must not carry a canary (this shell and its children excepted)
if [ "$mode" = deployed ]; then
    report canary_not_in_readable_proc_environ SKIP "$skipped"
else
    leak=""
    for e in /proc/[0-9]*/environ; do
        p=${e#/proc/}; p=${p%/environ}
        [ "$p" = "$$" ] && continue
        pp=$(awk '/^PPid:/ {print $2}' "/proc/$p/status" 2>/dev/null)
        [ "$pp" = "$$" ] && continue
        grep -qaF -e "$pat" "$e" 2>/dev/null
        if [ $? -eq 0 ]; then leak="$leak $p"; fi
    done
    if [ -z "$leak" ]; then report canary_not_in_readable_proc_environ PASS "no readable foreign environment holds a canary"; else report canary_not_in_readable_proc_environ FAIL "pids:$leak"; fi
fi

# --- no .env file in the image (site-packages carries library files of its own and is skipped)
envfile=$(find / -xdev \( -path /proc -o -path /sys -o -path '*/site-packages' -o -path '*/dist-packages' \) -prune -o -type f \( -name .env -o -name '.env.*' \) ! -name '*.example' ! -name '*.sample' ! -name '*.template' -print 2>/dev/null | head -n 5)
if [ -z "$envfile" ]; then report no_env_file PASS "none"; else report no_env_file FAIL "found: $envfile"; fi

# --- the upload parse worker, started the way the sandbox starts it (python part: one line per check, "RESULT <name> <status> <detail>")
run_python() {
python - "$SECRET_NAMES" <<'PY'
import json
import os
import sys

from semigraph.uploads import parse, sandbox

names = sys.argv[1].split()
prefix = "CANARY" + "_"
FIXTURE = b"Containment fixture.\n\nThis fixture paragraph is plain text for the image check.\n"
# The real worker entry point, run as __main__ in a child with the same command line flags and environment as an upload job. The
# child wraps apply_sandbox_limits (the first statement of the worker's __main__ guard) to report the state it leaves behind.
CHILD = """
import json, os, runpy, sys, threading
import semigraph.uploads.sandbox as sb
orig = sb.apply_sandbox_limits
def wrapped(*a, **k):
    applied = orig(*a, **k)
    status = open("/proc/self/status").read().splitlines()
    limits = open("/proc/self/limits").read().splitlines()
    fork_refused = False
    try:
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
    except BlockingIOError:
        fork_refused = True
    thread_refused = False
    try:
        threading.Thread(target=lambda: None).start()
    except RuntimeError:
        thread_refused = True
    info = {"applied": applied, "fork_refused": fork_refused, "thread_refused": thread_refused,
            "nnp": [l for l in status if l.startswith("NoNewPrivs")],
            "nproc": [l for l in limits if l.startswith("Max processes")],
            "proc_uid": os.stat("/proc/self").st_uid,
            "env": dict(os.environ)}
    sys.stdout.write("INFO " + json.dumps(info) + "\\n")
    sys.stdout.flush()
    return applied
sb.apply_sandbox_limits = wrapped
sys.argv = ["parse_worker", "txt", "5"]
runpy.run_module("semigraph.uploads.parse_worker", run_name="__main__")
"""


def result(name, ok, detail=""):
    print("RESULT %s %s %s" % (name, "PASS" if ok else "FAIL", " ".join(str(detail).split())[:300]))


def snapshot(root):
    seen = set()
    # hidden top-level entries (a cache directory the live server may create next to the worker's run) are not the worker's
    for directory, dirs, files in os.walk(root):
        if directory == root:
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            files = [f for f in files if not f.startswith(".")]
        for entry in dirs + files:
            seen.add(os.path.join(directory, entry))
    return seen


cmd = parse._worker_command("txt", 5)
result("worker_cmd_flags", "-s" in cmd and "-P" in cmd and "semigraph.uploads.parse_worker" in cmd, "argv " + " ".join(cmd[1:5]))

before = snapshot("/tmp")
info = None
parsed_ok = False
detail = ""
try:
    res = sandbox.run_sandboxed_command([sys.executable, "-s", "-P", "-c", CHILD], FIXTURE, timeout_s=60,
                                        max_output_bytes=1 << 20, env=parse._child_env())
    first, _, rest = res.stdout.decode("utf-8", "replace").partition("\n")
    info = json.loads(first[len("INFO "):]) if first.startswith("INFO ") else None
    doc = json.loads(rest) if rest.strip() else {}
    parsed_ok = res.returncode == 0 and "error" not in doc and "fixture paragraph" in json.dumps(doc)
    detail = "returncode %s, error %s" % (res.returncode, doc.get("error"))
except Exception as exc:  # noqa: BLE001 - every failure is a FAIL line, never a crash
    detail = type(exc).__name__
try:
    pdoc = parse.parse_document(FIXTURE, "txt")
    api_ok = bool(pdoc.blocks)
except Exception as exc:  # noqa: BLE001
    api_ok = False
    detail += " parse_document " + type(exc).__name__
result("worker_parse_ok", parsed_ok and api_ok, detail or "worker and parse_document both parsed the fixture")

if info is None:
    for name in ("worker_env_scrubbed", "worker_no_new_privs", "worker_nproc_zero", "worker_fork_refused", "worker_proc_root_owned"):
        result(name, False, "the worker reported no state (apply_sandbox_limits not reached)")
else:
    env = info["env"]
    bad = [k for k in env if k.upper() in names or any(w in k.upper() for w in ("KEY", "TOKEN", "SECRET", "PASSWORD", "PEPPER", "SALT", "AUTH"))]
    bad += [k for k, v in env.items() if prefix in v]
    result("worker_env_scrubbed", not bad, "variables: " + ",".join(sorted(env)) if not bad else "leaked: " + ",".join(bad))
    nnp = info["nnp"]
    result("worker_no_new_privs", info["applied"] is True and len(nnp) == 1 and nnp[0].split()[-1] == "1", nnp)
    limit = info["nproc"]
    result("worker_nproc_zero", len(limit) == 1 and limit[0].split()[2:4] == ["0", "0"], limit)
    result("worker_fork_refused", info["fork_refused"] is True and info["thread_refused"] is True,
           "fork refused %s, thread start refused %s" % (info["fork_refused"], info["thread_refused"]))
    result("worker_proc_root_owned", info["proc_uid"] == 0, "worker /proc entry uid %s" % info["proc_uid"])

after = snapshot("/tmp")
result("worker_leaves_no_tmp", not (after - before), "new: %s" % sorted(after - before)[:5] if after - before else "no new entry in /tmp")
PY
}
py_out=$(run_python 2>&1)
py_rc=$?
if [ "$py_rc" -ne 0 ]; then
    printf 'python part exited %s:\n%s\n' "$py_rc" "$py_out" | tail -n 12
    fails=$((fails + 1))
fi
while read -r tag name status detail; do
    [ "$tag" = "RESULT" ] || continue
    report "$name" "$status" "$detail"
done <<EOF
$py_out
EOF

# --- file-system facts of the image
sug=$(find / -xdev -perm /6000 -type f -print -quit 2>/dev/null)
if [ -z "$sug" ]; then report no_setuid PASS "no setuid or setgid file"; else report no_setuid FAIL "found $sug"; fi

# nothing on the import path, under /srv or /opt/semigraph, or in any parent of those, is writable by this user
wr=""
for p in $(python -c 'import sys; print(*sys.path)') /srv /opt/semigraph; do
    if [ -e "$p" ]; then
        w=$(find "$p" -writable -print -quit 2>/dev/null)
        [ -z "$w" ] || wr="$wr $w"
    fi
    while [ "$p" != / ]; do
        if [ -w "$p" ]; then wr="$wr $p"; fi
        p=$(dirname "$p")
    done
done
if [ -z "$wr" ]; then report nothing_writable PASS "import path, /srv, /opt/semigraph and their parents"; else report nothing_writable FAIL "writable:$wr"; fi

# the dist (R13, owner decision 5): root's, no group or other write bit, in the dist and in /opt and /opt/semigraph above it
bad=$(find /opt /opt/semigraph /opt/semigraph/web -maxdepth 0 \( ! -user root -o -perm /022 \) -print 2>/dev/null; find /opt/semigraph \( ! -user root -o -perm /022 \) -print -quit 2>/dev/null)
if [ -z "$bad" ] && [ -d /opt/semigraph/web ]; then report dist_root_owned PASS "$(stat -c '%U:%G %a %n' /opt /opt/semigraph /opt/semigraph/web | tr '\n' ';')"; else report dist_root_owned FAIL "not root-only: $bad"; fi
unreadable=$(find /opt/semigraph/web \( \( -type f ! -perm -004 \) -o \( -type d ! -perm -005 \) \) -print -quit 2>/dev/null)
if [ -r /opt/semigraph/web/index.html ] && [ -z "$unreadable" ]; then report dist_readable PASS "index.html readable, no entry hidden from others"; else report dist_readable FAIL "index.html unreadable or entry not world-readable: $unreadable"; fi
distpath=$(cd /srv && python -c 'from semigraph.config import Settings; print(Settings().ui_dist_dir)' 2>/dev/null)
if [ "$distpath" = "/opt/semigraph/web" ]; then report dist_path_default PASS "$distpath"; else report dist_path_default FAIL "effective dist path '$distpath'"; fi

srv=$(find /srv /srv/models -maxdepth 0 \( ! -user root -o -perm /022 \) -print 2>/dev/null)
if [ -z "$srv" ]; then report srv_root_owned PASS "/srv and /srv/models are root-only"; else report srv_root_owned FAIL "$srv"; fi

if [ "$PYTHONSAFEPATH" = "1" ] && [ "$PYTHONNOUSERSITE" = "1" ]; then report python_env_flags PASS "PYTHONSAFEPATH=1 PYTHONNOUSERSITE=1"; else report python_env_flags FAIL "PYTHONSAFEPATH='$PYTHONSAFEPATH' PYTHONNOUSERSITE='$PYTHONNOUSERSITE'"; fi

# no Node in the runtime image (spec Acceptance, verifier Warning 4): not on PATH and no executable of that name anywhere
# (library directories carry files of their own and are skipped, as for the .env search)
nodes=""
for n in node nodejs npm npx; do
    if command -v "$n" >/dev/null 2>&1; then nodes="$nodes $n"; fi
done
nodefile=$(find / -xdev \( -path /proc -o -path /sys -o -path '*/site-packages' -o -path '*/dist-packages' \) -prune -o -type f \( -name node -o -name nodejs -o -name npm -o -name npx \) -perm /111 -print -quit 2>/dev/null)
if [ -z "$nodes" ] && [ -z "$nodefile" ]; then report no_node PASS "no node, nodejs, npm or npx"; else report no_node FAIL "on PATH:$nodes file: $nodefile"; fi

# The workspace store lives in the database, not on disk: the serving code writes nothing under /srv and its temporary files go to
# /tmp. This asserts that layout (no workspace directory a job of another workspace could read); the /tmp comparison above shows a
# parse leaves nothing behind.
ws=$(find /tmp /var/tmp /srv /opt/semigraph /nonexistent -iname '*workspace*' -print -quit 2>/dev/null)
if [ -z "$ws" ]; then report workspace_store_none PASS "no on-disk workspace store; per-workspace data lives in the database"; else report workspace_store_none FAIL "found $ws"; fi

finish
