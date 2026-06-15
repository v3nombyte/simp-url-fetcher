"""Cross-platform install script for Simp URL Fetcher.

Usage:
    python install.py

Creates a virtual environment, installs dependencies, sets up Playwright,
and copies env.example → .env if needed.
"""

import os
import subprocess
import sys
import venv
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent


def note(msg: str) -> None:
    print(f"  • {msg}")


def run(cmd: list[str], desc: str = "") -> int:
    if desc:
        print(f"[{desc}]")
    result = subprocess.run(cmd, capture_output=False)
    return result.returncode


def main() -> int:
    print("=== Simp URL Fetcher Install ===\n")
    os.chdir(str(PROJECT_DIR))

    # ── Check Python version ──
    py_version = sys.version_info
    if py_version >= (3, 14):
        print("WARNING: Python 3.14 has removed asyncio.coroutine which some")
        print("  dependencies still use. Python 3.10–3.13 is recommended.")
        print(f"  You are running: {sys.version}\n")

    # ── 1. Virtual environment ──
    venv_path = PROJECT_DIR / "venv"
    bin_dir = venv_path / ("Scripts" if sys.platform == "win32" else "bin")
    pip_exe = bin_dir / ("pip.exe" if sys.platform == "win32" else "pip")
    python_exe = bin_dir / ("python.exe" if sys.platform == "win32" else "python")

    if venv_path.is_dir():
        print("[1/4] Virtual environment already exists — skipping")
    else:
        print("[1/4] Creating Python virtual environment...")
        venv.create(str(venv_path), with_pip=True)

    # ── 2. Install dependencies ──
    print("[2/4] Installing Python dependencies...")
    run([str(pip_exe), "install", "--upgrade", "pip", "-q"],
        desc="  pip upgrade")
    if (PROJECT_DIR / "requirements.txt").exists():
        run([str(pip_exe), "install", "-r", str(PROJECT_DIR / "requirements.txt"), "-q"],
            desc="  requirements.txt")
    else:
        note("No requirements.txt found — skipping")

    # ── 3. Playwright browsers ──
    print("[3/4] Installing Playwright Chromium browser...")
    playwright_result = subprocess.run(
        [str(python_exe), "-m", "playwright", "install", "chromium"],
        capture_output=True, text=True,
    )
    if playwright_result.returncode != 0:
        note(f"Playwright install had warnings (non-fatal): {playwright_result.stderr.strip()[-200:]}")

    # ── 4. Environment file ──
    env_file = PROJECT_DIR / ".env"
    env_example = PROJECT_DIR / "env.example"
    if not env_file.exists():
        if env_example.exists():
            print("[4/4] Creating .env from env.example...")
            env_file.write_text(env_example.read_text())
            note("Edit .env with your JDownloader credentials before running")
        else:
            note("No env.example found — skipping .env creation")
    else:
        print("[4/4] .env already exists — skipping")

    print("\nInstall complete.")
    print(f"Run  python {PROJECT_DIR / 'start.py'}  to launch the app.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
