"""Removed executor configurations remain readable but cannot execute or save."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from AutoGLM_GUI.agents.factory import create_agent, list_agent_types
from AutoGLM_GUI.api.agents import router
from AutoGLM_GUI.config import AgentConfig, ModelConfig
from AutoGLM_GUI.config_manager import ConfigModel, UnifiedConfigManager
from AutoGLM_GUI.types import SUPPORTED_AGENT_TYPES

pytestmark = [pytest.mark.contract, pytest.mark.release_gate]
REMOVED_TYPES = ("mai", "droidrun", "midscene")


def _manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_type: str
) -> tuple[UnifiedConfigManager, Path]:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "agent_type": agent_type,
                "agent_config_params": {"history_n": 7, "model_family": "old"},
                "base_url": "https://model.example/v1",
                "model_name": "custom-vision-model",
                "temperature": 1.0,
            }
        )
    )
    monkeypatch.setattr(UnifiedConfigManager, "_instance", None)
    monkeypatch.setattr(UnifiedConfigManager, "_config_path", path)
    for name in tuple(os.environ):
        if name.startswith("AUTOGLM_"):
            monkeypatch.delenv(name)
    manager = UnifiedConfigManager()
    manager.load_file_config(force_reload=True)
    return manager, path


@pytest.mark.parametrize("agent_type", REMOVED_TYPES)
def test_removed_config_read_preserves_type_and_other_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_type: str
) -> None:
    manager, path = _manager(tmp_path, monkeypatch, agent_type)
    before = path.read_bytes()
    config = manager.get_effective_config()

    assert config.agent_type == agent_type
    assert config.agent_type_supported is False
    assert config.model_name == "custom-vision-model"
    assert config.temperature == 1.0
    assert config.agent_config_params == {"history_n": 7, "model_family": "old"}
    assert path.read_bytes() == before


@pytest.mark.parametrize("agent_type", REMOVED_TYPES)
def test_removed_config_save_is_rejected_without_rewriting_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_type: str
) -> None:
    manager, path = _manager(tmp_path, monkeypatch, agent_type)
    before = path.read_bytes()
    assert not manager.save_file_config(
        base_url="https://other.example/v1",
        model_name="other",
        agent_type=agent_type,
    )
    # Omitting the type must not silently preserve an unusable selection on save.
    assert not manager.save_file_config(
        base_url="https://other.example/v1", model_name="other"
    )
    assert path.read_bytes() == before


@pytest.mark.parametrize("agent_type", REMOVED_TYPES)
def test_switching_from_removed_type_clears_options_even_from_stale_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_type: str
) -> None:
    manager, path = _manager(tmp_path, monkeypatch, agent_type)
    assert manager.save_file_config(
        base_url="https://model.example/v1",
        model_name="supported-vision-model",
        agent_type="qwen",
        agent_config_params={"history_n": 7, "model_family": "old"},
    )
    persisted = json.loads(path.read_text())
    assert persisted["agent_type"] == "qwen"
    assert persisted["agent_config_params"] == {}
    assert manager.get_effective_config().agent_type_supported is True


@pytest.mark.parametrize("agent_type", REMOVED_TYPES)
def test_removed_factory_type_fails_before_model_or_device_initialization(
    agent_type: str,
) -> None:
    with pytest.raises(ValueError, match="no longer supported"):
        create_agent(
            agent_type,
            ModelConfig(),
            AgentConfig(),
            {},
            SimpleNamespace(),
        )


def test_supported_registry_keeps_existing_aliases() -> None:
    assert set(list_agent_types()) == SUPPORTED_AGENT_TYPES
    for name in SUPPORTED_AGENT_TYPES:
        assert ConfigModel(agent_type=name).agent_type_supported


def test_original_glm_name_is_normalized_on_read_and_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, path = _manager(tmp_path, monkeypatch, "glm")
    assert ConfigModel(agent_type="glm").agent_type == "glm-async"
    assert manager.get_effective_config().agent_type == "glm-async"
    assert manager.save_file_config(
        base_url="https://model.example/v1",
        model_name="custom-vision-model",
        agent_type="glm",
    )
    assert json.loads(path.read_text())["agent_type"] == "glm-async"


@pytest.mark.parametrize("agent_type", REMOVED_TYPES)
def test_config_api_exposes_unsupported_selection_and_rejects_saving_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_type: str
) -> None:
    import AutoGLM_GUI.config_manager as config_module

    manager, path = _manager(tmp_path, monkeypatch, agent_type)
    monkeypatch.setattr(config_module, "config_manager", manager)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    before = path.read_bytes()

    response = client.get("/api/config")
    assert response.status_code == 200
    assert response.json()["agent_type"] == agent_type
    assert response.json()["agent_type_supported"] is False
    saved = client.post(
        "/api/config",
        json={
            "base_url": "https://model.example/v1",
            "model_name": "custom-vision-model",
            "agent_type": agent_type,
        },
    )
    assert saved.status_code == 422
    omitted_type = client.post(
        "/api/config",
        json={
            "base_url": "https://model.example/v1",
            "model_name": "custom-vision-model",
        },
    )
    assert omitted_type.status_code == 422
    assert path.read_bytes() == before
