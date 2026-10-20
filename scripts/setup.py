#!/usr/bin/env python3
"""Prepare a checkout: uv run --no-project python scripts/setup.py."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from build import copy_static_files, get_backend_version

ROOT_DIR = Path(__file__).resolve().parent.parent


def main() -> int:
    executables = {name: shutil.which(name) for name in ("uv", "node", "pnpm")}
    missing = [name for name, executable in executables.items() if not executable]
    if missing:
        print(f"Missing required tools: {', '.join(missing)}", file=sys.stderr)
        return 1

    env = os.environ.copy()
    # An inherited override must not sync dependencies into another checkout.
    env["UV_PROJECT_ENVIRONMENT"] = str(ROOT_DIR / ".venv")
    env["VITE_BACKEND_VERSION"] = get_backend_version()
    commands = [
        (ROOT_DIR, [executables["uv"], "sync", "--locked", "--dev"]),
        (ROOT_DIR / "frontend", [executables["pnpm"], "install", "--frozen-lockfile"]),
        (ROOT_DIR / "electron", [executables["pnpm"], "install", "--frozen-lockfile"]),
        (ROOT_DIR / "frontend", [executables["pnpm"], "build"]),
    ]
    try:
        for cwd, command in commands:
            print(f"Setting up {cwd.name}: {' '.join(command)}", flush=True)
            subprocess.run(command, cwd=cwd, env=env, check=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1

    if not copy_static_files():
        return 1
    if not shutil.which("adb"):
        print(
            "Note: install ADB and add it to PATH before controlling Android devices."
        )
    print("Setup complete. Start the backend with: uv run autoglm-gui --reload")
    return 0


if __name__ == "__main__":
    sys.exit(main())
