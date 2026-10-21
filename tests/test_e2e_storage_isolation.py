"""Concurrent test backends must not share settings or touch the user's home."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.start_e2e_services import _run_autoglm_server as run_browser_backend
from tests.e2e.conftest import _run_autoglm_server as run_test_backend


@pytest.mark.parametrize("browser", [True, False])
def test_backend_launches_get_private_cleaned_storage(monkeypatch, browser):
    original_home = os.environ.get("HOME")
    storage_roots = []

    def serve(*args, **kwargs):
        storage_root = Path.home()
        settings = storage_root / ".config" / "autoglm" / "config.json"
        assert not settings.exists()
        settings.parent.mkdir(parents=True)
        settings.write_text('{"model_name":"test-only"}')
        storage_roots.append(storage_root)
        assert os.environ.get("HOME") == original_home

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=serve))
    monkeypatch.setitem(
        sys.modules, "AutoGLM_GUI.server", SimpleNamespace(app=object())
    )
    monkeypatch.delenv("COVERAGE_PROCESS_START", raising=False)
    # Restore environment settings changed by each test launcher afterwards.
    for key in (
        "AUTOGLM_BASE_URL",
        "AUTOGLM_MODEL_NAME",
        "AUTOGLM_API_KEY",
        "AUTOGLM_TRACE_ENABLED",
        "AUTOGLM_TRACE_REPLAY_ENABLED",
        "AUTOGLM_TRACE_CAPTURE_SCREENSHOT",
        "AUTOGLM_TRACE_FILE",
    ):
        monkeypatch.setenv(key, os.environ.get(key, ""))

    for _ in range(2):
        if browser:
            run_browser_backend(8000, "http://127.0.0.1:1")
        else:
            run_test_backend(8000, "http://127.0.0.1:1", "/tmp/test-trace.jsonl")

    assert storage_roots[0] != storage_roots[1]
    assert all(not root.exists() for root in storage_roots)
    assert os.environ.get("HOME") == original_home
