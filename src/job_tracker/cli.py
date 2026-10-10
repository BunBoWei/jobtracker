"""The `jobs` command. Starts the server on demand and stops it on request; nothing runs at boot."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import webbrowser
from pathlib import Path


def port() -> int:
    return int(os.environ.get("JOBS_PORT", "8765"))


def url() -> str:
    return f"http://localhost:{port()}"


def log_file() -> Path:
    return Path(os.environ.get("JOBS_LOG_FILE") or Path.home() / "Library/Logs/job-tracker.log")


def open_browser(target: str) -> None:
    webbrowser.open(target)


def is_running() -> bool:
    """True only if our own app answers /health (a foreign program on the port does not count)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port()}/health", timeout=1) as resp:
            return json.load(resp).get("app") == "job-tracker"
    except (OSError, ValueError, http.client.HTTPException):
        return False


def start_server() -> None:
    log = log_file()
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "job_tracker.server", "--port", str(port())],
            stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
        )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if is_running():
            return
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    raise SystemExit(f"The server did not start. See {log}")


def _listener_pids() -> list[int]:
    out = subprocess.run(["lsof", "-ti", f"tcp:{port()}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout
    return sorted({int(p) for p in out.split()})


def _alive(pid: int) -> bool:
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")  # a zombie is already dead


def stop_server() -> bool:
    """SIGTERM the server and wait until it has exited. uvicorn finishes in-flight requests first."""
    if not is_running():
        return False
    pids = _listener_pids()
    if not pids:
        raise SystemExit("Job Tracker is running but its process could not be found; stop it from Activity Monitor.")
    for pid in pids:
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if not any(_alive(p) for p in pids):
            return True
        time.sleep(0.1)
    raise SystemExit("Job Tracker did not stop within 30 seconds.")


def launcher_source(jobs_path: str) -> str:
    escaped = jobs_path.replace("\\", "\\\\").replace('"', '\\"')
    return f'do shell script quoted form of "{escaped}"\n'


def install_launcher() -> Path:
    jobs_path = shutil.which("jobs") or str(Path(sys.argv[0]).resolve())
    dest = Path.home() / "Applications" / "Job Tracker.app"
    dest.parent.mkdir(exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)  # only ever our own launcher, at this exact path
    with tempfile.NamedTemporaryFile("w", suffix=".applescript", delete=False) as f:
        f.write(launcher_source(jobs_path))
    try:
        subprocess.run(["osacompile", "-o", str(dest), f.name], check=True)
    finally:
        os.unlink(f.name)
    return dest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="jobs", description="Local job application tracker")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("stop", help="stop the server")
    sub.add_parser("status", help="say whether the server is running")
    sub.add_parser("install-launcher", help="create ~/Applications/Job Tracker.app for Spotlight")
    args = parser.parse_args(argv)

    if args.cmd == "stop":
        print("Stopped." if stop_server() else "Job Tracker is not running.")
    elif args.cmd == "status":
        print(f"Job Tracker is running at {url()}" if is_running() else "Job Tracker is not running.")
    elif args.cmd == "install-launcher":
        print(f"Created {install_launcher()}. Press Cmd+Space and type 'Job Tracker'.")
    else:
        if not is_running():
            start_server()
        open_browser(url())
    return 0


if __name__ == "__main__":
    sys.exit(main())
