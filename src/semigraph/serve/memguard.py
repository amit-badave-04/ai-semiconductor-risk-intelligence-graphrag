"""Upload memory guard and the process memory log line (docs/specs/upload-memory-guard.md, M5a decision 21).

The fp32 query embedder adds about 1.26 GB of private memory to a 4 GB machine. Two free safeguards ship with it:

* :func:`check_upload` refuses a NEW upload while ``MemAvailable`` (``/proc/meminfo``) is below
  ``Settings.upload_min_mem_available_kb`` (``workspace_routes`` answers 503 and starts nothing);
* :func:`log_memory` writes one INFO line, ``memory vmrss_kb=... vmhwm_kb=... mem_available_kb=... swap_used_kb=...``,
  for the serving process (the maintenance thread calls it every 60 s), so the live margin is visible in the logs.

The guard is a safeguard, not a security control, so it fails OPEN: where ``/proc/meminfo`` does not exist (Windows, a test, a
non-Linux host) it is off and :func:`log_boot_posture` says so once at boot; on a host that has the file, a read error or an
unreadable line accepts that upload with one WARNING and never refuses it. Nothing here is per visitor: the only input is the
number the machine reports and a setting, no request data reaches a file path (the two paths are the constants below,
read at call time so a test can point them at files of its own), and no log line carries an address, a workspace id, a file
name or any upload content.

Standard library only: this module is imported by the serve-shipped image.
"""

import logging
import os

logger = logging.getLogger("semigraph.serve.memguard")

MEMINFO_PATH = "/proc/meminfo"
STATUS_PATH = "/proc/self/status"


def _read_kb_fields(path: str, names: tuple[str, ...]) -> dict[str, int]:
    """``{name: kilobytes}`` for each of ``names`` found in the ``/proc`` file at ``path`` (lines such as
    ``MemAvailable:    1471596 kB``); a name that is not in the file is left out. Raises ``OSError`` when the file cannot be
    read (``FileNotFoundError`` where it does not exist) and ``ValueError`` for a line whose value is not a number."""
    found: dict[str, int] = {}
    with open(path, encoding="ascii", errors="replace") as handle:
        for line in handle:
            name, separator, rest = line.partition(":")
            if separator and name in names:
                fields = rest.split()
                if not fields:
                    raise ValueError(f"{name} has no value")
                found[name] = int(fields[0])
    return found


def read_mem_available_kb() -> int:
    """``MemAvailable`` from ``/proc/meminfo``, in kB: what the kernel estimates can be started without swapping. Raises
    ``FileNotFoundError`` off Linux, other ``OSError`` for a file that cannot be read and ``ValueError`` when the line is
    absent or is not a number."""
    fields = _read_kb_fields(MEMINFO_PATH, ("MemAvailable",))
    if "MemAvailable" not in fields:
        raise ValueError("MemAvailable is missing from the meminfo file")
    return fields["MemAvailable"]


def refuses(mem_available_kb: int, threshold_kb: int) -> bool:
    """The decision: refuse only when strictly below the threshold (equal is accepted); a threshold of 0 or less is off."""
    return threshold_kb > 0 and mem_available_kb < threshold_kb


def check_upload(threshold_kb: int) -> bool:
    """True when a new upload must be refused for lack of memory. Reads ``/proc/meminfo`` only when the guard is on
    (``threshold_kb`` above 0). Off (False, silently) where the file does not exist; any other failure is False with one
    WARNING naming only the exception type. A refusal logs one WARNING with the two figures (never sent to the client)."""
    if threshold_kb <= 0:
        return False
    try:
        available_kb = read_mem_available_kb()
    except FileNotFoundError:
        return False
    except Exception as error:  # noqa: BLE001 - the guard is a safeguard: a fault of its own must never block an upload
        logger.warning("upload memory guard unavailable for this request (%s): the upload is accepted", type(error).__name__)
        return False
    if not refuses(available_kb, threshold_kb):
        return False
    logger.warning("upload refused: memory mem_available_kb=%d threshold_kb=%d", available_kb, threshold_kb)
    return True


def log_boot_posture() -> None:
    """One WARNING at boot when the host has no ``/proc/meminfo`` (the guard is then off for every upload)."""
    if not os.path.exists(MEMINFO_PATH):
        logger.warning("upload memory guard is OFF: /proc/meminfo is not available on this host")


def memory_line() -> str | None:
    """The memory log line for this process, or None where ``/proc`` does not exist. Raises ``ValueError`` when a file
    that exists lacks ``MemAvailable``, ``VmRSS`` or ``VmHWM`` (a host without swap support has no swap lines: swap used is
    then 0). ``swap_used_kb`` is the host's swap in use; ``vmrss_kb`` and ``vmhwm_kb`` are this process's resident and peak
    resident memory."""
    try:
        host = _read_kb_fields(MEMINFO_PATH, ("MemAvailable", "SwapTotal", "SwapFree"))
        process = _read_kb_fields(STATUS_PATH, ("VmRSS", "VmHWM"))
    except FileNotFoundError:
        return None
    missing = [name for name, fields in (("MemAvailable", host), ("VmRSS", process), ("VmHWM", process))
               if name not in fields]
    if missing:
        raise ValueError("missing from the /proc files: " + ", ".join(missing))
    swap_used_kb = max(host.get("SwapTotal", 0) - host.get("SwapFree", 0), 0)
    return (f"memory vmrss_kb={process['VmRSS']} vmhwm_kb={process['VmHWM']} mem_available_kb={host['MemAvailable']} "
            f"swap_used_kb={swap_used_kb}")


def log_memory() -> None:
    """Write :func:`memory_line` at INFO (nothing where ``/proc`` does not exist). An error propagates: the maintenance
    thread logs it and tries again at the next tick."""
    line = memory_line()
    if line is not None:
        logger.info("%s", line)
