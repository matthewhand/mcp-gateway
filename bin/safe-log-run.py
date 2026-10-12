#!/usr/bin/env python3
"""Run a service with resilient file logging.

Problem this solves
-------------------
systemd `StandardOutput=append:/path/to.log` ties process health to the log
filesystem. When the disk hits ENOSPC, the first write to stdout/stderr can
kill the service (Python often exits 120). OAuth and other long-running
bridges then flap or stay dead behind nginx 502s.

This wrapper:
  1. Runs disk-guard pre-flight (best-effort reclaim)
  2. Starts the real command with stdout+stderr piped here
  3. Appends to LOGPATH, but NEVER lets log I/O kill the child
     - on ENOSPC: truncate the log once and retry; else drop the chunk
     - on oversized log: self-cap before append
  4. Forwards SIGTERM/SIGINT/SIGHUP to the child and exits with its code

Usage:
  safe-log-run.py /path/to/file.log -- /usr/bin/python3 /path/app.py ...
"""
from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import threading
import time

SOFT_MAX = int(os.environ.get("MCP_LOG_SOFT_MAX_MB", "50")) * 1024 * 1024
# Seconds to wait after forwarding SIGTERM/SIGINT before escalating to SIGKILL.
# Must stay below the unit's TimeoutStopSec (20s for mcp-cli-server@) so systemd
# does not SIGKILL the wrapper at the same moment we escalate.
TERM_GRACE = float(os.environ.get("MCP_LOG_TERM_GRACE", "10"))
DISK_GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "disk-guard.sh")


def _eprint(msg: str) -> None:
    """Best-effort message to systemd journal (wrapper's own stderr)."""
    try:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def preflight_disk_guard() -> None:
    if not os.path.isfile(DISK_GUARD):
        return
    try:
        subprocess.run(
            [DISK_GUARD, "--quiet"],
            timeout=60,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        _eprint(f"[safe-log-run] disk-guard preflight failed: {e!r}")


def safe_append(path: str, data: bytes) -> None:
    if not data:
        return
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        pass

    def _write(mode: str, payload: bytes) -> None:
        with open(path, mode) as f:
            f.write(payload)
            f.flush()

    # Soft size cap (copytruncate-ish) so a single process can't re-fill the disk
    try:
        if os.path.exists(path) and os.path.getsize(path) >= SOFT_MAX:
            marker = (
                f"\n[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] "
                f"safe-log-run: truncated log (exceeded {SOFT_MAX} bytes)\n"
            ).encode()
            try:
                _write("wb", marker)
            except OSError:
                pass
    except OSError:
        pass

    try:
        _write("ab", data)
        return
    except OSError as e:
        if e.errno != errno.ENOSPC:
            _eprint(f"[safe-log-run] log write error: {e!r}")
            return

    # ENOSPC: emergency truncate + single retry of a short tail of the chunk
    _eprint(f"[safe-log-run] ENOSPC writing {path}; truncating and retrying once")
    try:
        marker = (
            f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] "
            f"safe-log-run: truncated due to ENOSPC\n"
        ).encode()
        _write("wb", marker + data[-8192:])
    except OSError as e2:
        _eprint(f"[safe-log-run] ENOSPC persists; dropping log chunk: {e2!r}")


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] == "-h" or argv[1] == "--help":
        sys.stderr.write(
            "usage: safe-log-run.py LOGPATH -- COMMAND [ARGS...]\n"
        )
        return 2

    logpath = argv[1]
    rest = argv[2:]
    if rest and rest[0] == "--":
        rest = rest[1:]
    if not rest:
        sys.stderr.write("safe-log-run.py: missing COMMAND\n")
        return 2

    preflight_disk_guard()

    try:
        proc = subprocess.Popen(
            rest,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            # Own the child's process group. The tree is several levels deep
            # (safe-log-run -> uv run -> python, and deeper for CLI/glama via
            # npx/uvx). Signalling the direct child alone relies on every
            # intermediate relay forwarding, which is not guaranteed — a
            # grandchild that ignores SIGTERM leaves this wrapper blocked in
            # proc.wait() forever and the unit stuck in stop-sigterm. With its
            # own session we can signal the whole group directly.
            start_new_session=True,
        )
    except OSError as e:
        _eprint(f"[safe-log-run] failed to spawn {rest!r}: {e!r}")
        return 127

    assert proc.stdout is not None

    def reader() -> None:
        try:
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                safe_append(logpath, chunk)
        except Exception as e:
            _eprint(f"[safe-log-run] reader stopped: {e!r}")

    t = threading.Thread(target=reader, name="safe-log-reader", daemon=True)
    t.start()

    # start_new_session=True makes the child a session/pgroup leader, so its
    # pgid equals its pid. Using proc.pid avoids a getpgid TOCTOU window.
    pgid = proc.pid

    def signal_group(sig: int) -> None:
        # POSIX-only by design: every caller is a systemd user unit on Linux.
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def escalate(deadline: float) -> None:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            time.sleep(min(0.25, left))
        if proc.poll() is None:
            _eprint(
                f"[safe-log-run] child group {pgid} ignored SIGTERM for "
                f"{TERM_GRACE}s; sending SIGKILL"
            )
            signal_group(signal.SIGKILL)

    def forward(signum: int, _frame) -> None:
        signal_group(signum)
        if signum in (signal.SIGTERM, signal.SIGINT):
            deadline = time.monotonic() + TERM_GRACE
            threading.Thread(
                target=escalate, args=(deadline,), daemon=True
            ).start()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, forward)
        except Exception as e:  # noqa: BLE001
            # A failed registration leaves the default disposition in place,
            # so the wrapper would die on SIGTERM and orphan the child tree.
            _eprint(f"[safe-log-run] cannot install handler for {sig}: {e!r}")

    rc = proc.wait()
    t.join(timeout=3)
    # proc.wait() returns a negative value when the child died by signal;
    # map that to the conventional 128+signo shell status (e.g. -9 -> 137)
    # instead of leaking a nonsense exit code.
    if rc is None:
        return 1
    if rc < 0:
        return 128 + (-rc)
    return int(rc)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
