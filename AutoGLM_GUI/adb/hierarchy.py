"""Bounded, optional Android UI hierarchy reads for conditioned skills."""

import asyncio
import subprocess
import time
from uuid import uuid4

from AutoGLM_GUI.platform_utils import build_adb_command, run_cmd_silently_sync
from AutoGLM_GUI.trace import trace_span


def get_ui_hierarchy(device_id: str, timeout: int = 10) -> str:
    if not 1 <= timeout <= 30:
        raise ValueError("Hierarchy timeout must be between 1 and 30 seconds")
    prefix = build_adb_command(device_id)
    path = f"/data/local/tmp/autoglm-ui-{uuid4().hex}.xml"
    deadline = time.monotonic() + timeout

    def run(args: list[str]) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("UI hierarchy capture timed out")
        result = run_cmd_silently_sync(prefix + args, timeout=remaining)
        if result.returncode:
            raise RuntimeError("UI hierarchy capture unavailable")
        return result.stdout

    with trace_span("adb.capture_ui_hierarchy", attrs={"device_id": device_id}):
        try:
            run(["shell", "uiautomator", "dump", path])
            xml = run(["exec-out", "cat", path])
            if len(xml) > 2_000_000 or "<hierarchy" not in xml:
                raise ValueError("Invalid UI hierarchy")
            return xml
        finally:
            # Unique task-owned scratch file; never print the private XML.
            try:
                run_cmd_silently_sync(prefix + ["shell", "rm", "-f", path], timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass


async def get_ui_hierarchy_async(device_id: str, timeout: int = 10) -> str:
    # Cancellation abandons only this read. Its bounded worker still cleans its
    # unique scratch file; the adapter rejects evidence across action epochs.
    return await asyncio.to_thread(get_ui_hierarchy, device_id, timeout)
