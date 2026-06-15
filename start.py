"""Launcher that runs app.py as a subprocess and auto-restarts after updates.

Usage:
    python start.py

Detects the virtual environment, re-executes inside it if needed, then
enters a lifecycle loop: spawn app.py as a subprocess, wait for it to
exit, and restart if a ".restart.rqd" marker file was written (indicating
a successful update).
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
RESTART_MARKER = PROJECT_DIR / ".restart.rqd"


def restart_in_venv(venv_python: Path) -> None:
    """Re-run this script with the venv's Python interpreter (cross-platform)."""
    print(f"Using virtual environment: {venv_python.parent}")
    if sys.platform == "win32":
        subprocess.Popen([str(venv_python), __file__])
        sys.exit(0)
    else:
        os.execv(str(venv_python), [str(venv_python), __file__])


def main() -> int:
    os.chdir(str(PROJECT_DIR))

    # ── Detect / activate virtual environment ──
    venv_path = PROJECT_DIR / "venv"
    if sys.platform == "win32":
        venv_python = venv_path / "Scripts" / "python.exe"
    else:
        venv_python = venv_path / "bin" / "python"

    if not venv_path.is_dir():
        print("No virtual environment found — run  python install.py  first.")
        return 1

    if not sys.executable.startswith(str(venv_path.resolve())):
        if not venv_python.exists():
            print(f"Virtual environment is corrupt (missing {venv_python}) — re-run:  python install.py")
            return 1
        restart_in_venv(venv_python)
        return 0  # never reached

    # ── Check .env ──
    if not (PROJECT_DIR / ".env").exists():
        print("WARNING: No .env file found — JDownloader integration will be unavailable.")
        print("  Copy env.example → .env and fill in your credentials to enable it.\n")

    # Clean up any stale restart marker from a previous crash
    RESTART_MARKER.unlink(missing_ok=True)

    # ── Signal handler for Ctrl+C (kill child, then exit) ──
    child_proc: subprocess.Popen | None = None

    def _handle_sigint(signum, frame):
        print("\nShutting down...")
        if child_proc and child_proc.poll() is None:
            child_proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_sigint)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_sigint)

    # ── Lifecycle loop ──
    first_start = True
    while True:
        if first_start:
            first_start = False
        else:
            print("\n  Restarting after update...\n")
            # Brief pause so the old port is fully released
            time.sleep(1.0)

        print("\n" + "=" * 60)
        print("  Simp URL Fetcher")
        print("  http://localhost:8080")
        print("=" * 60 + "\n")

        child_proc = subprocess.Popen(
            [str(venv_python), "app.py"],
        )

        try:
            child_proc.wait()
        except KeyboardInterrupt:
            if child_proc.poll() is None:
                child_proc.terminate()
                child_proc.wait()

        if RESTART_MARKER.exists():
            RESTART_MARKER.unlink(missing_ok=True)
            continue  # restart the app subprocess

        print(f"App exited (code {child_proc.returncode})")
        break

    return 0


if __name__ == "__main__":
    sys.exit(main())
