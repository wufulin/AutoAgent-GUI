import json
import os

import pytest

from AutoGLM_GUI.config_manager import (
    TEMPERATURE_DEFAULT,
    ConfigModel,
    UnifiedConfigManager,
)


def _write_config(path, **overrides):
    data = {
        "base_url": "https://example.com/v1",
        "model_name": "autoglm-phone-9b",
    }
    data.update(overrides)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _manager_for(tmp_path, monkeypatch, **config):
    config_path = tmp_path / "config.json"
    _write_config(config_path, **config)
    monkeypatch.setattr(UnifiedConfigManager, "_instance", None)
    monkeypatch.setattr(UnifiedConfigManager, "_config_path", config_path)
    manager = UnifiedConfigManager()
    manager.load_file_config(force_reload=True)
    return manager, config_path


def test_temperature_default() -> None:
    assert TEMPERATURE_DEFAULT == 0.0
    assert ConfigModel().temperature == TEMPERATURE_DEFAULT


def test_temperature_accepts_k3_required_value() -> None:
    assert ConfigModel(temperature=1).temperature == 1.0


@pytest.mark.parametrize("value", [-0.1, 2.1])
def test_temperature_out_of_range_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="temperature must be between"):
        ConfigModel(temperature=value)


def test_temperature_env_var_parsing(tmp_path, monkeypatch) -> None:
    manager, _ = _manager_for(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTOGLM_TEMPERATURE", "1")
    manager.load_env_config()
    assert manager.get_effective_config().temperature == 1.0


def test_temperature_env_var_invalid(tmp_path, monkeypatch) -> None:
    manager, _ = _manager_for(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTOGLM_TEMPERATURE", "not-a-number")
    manager.load_env_config()
    assert manager.get_effective_config().temperature == TEMPERATURE_DEFAULT


def test_temperature_from_config_file(tmp_path, monkeypatch) -> None:
    manager, _ = _manager_for(tmp_path, monkeypatch, temperature=1)
    assert manager.get_effective_config().temperature == 1.0


def test_temperature_env_overrides_config_file(tmp_path, monkeypatch) -> None:
    manager, _ = _manager_for(tmp_path, monkeypatch, temperature=1)
    monkeypatch.setenv("AUTOGLM_TEMPERATURE", "0.5")
    manager.load_env_config()
    assert manager.get_effective_config().temperature == 0.5


def test_temperature_synced_to_env(tmp_path, monkeypatch) -> None:
    manager, _ = _manager_for(tmp_path, monkeypatch, temperature=1)
    synced = (
        "AUTOGLM_BASE_URL",
        "AUTOGLM_MODEL_NAME",
        "AUTOGLM_API_KEY",
        "AUTOGLM_TEMPERATURE",
    )
    try:
        manager.sync_to_env()
        assert os.environ["AUTOGLM_TEMPERATURE"] == "1.0"
    finally:
        for name in synced:
            os.environ.pop(name, None)


def test_save_file_config_preserves_temperature(tmp_path, monkeypatch) -> None:
    manager, config_path = _manager_for(tmp_path, monkeypatch, temperature=1)

    saved = manager.save_file_config(
        base_url="https://example.com/v1",
        model_name="autoglm-phone-9b",
        merge_mode=True,
    )

    assert saved is True
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["temperature"] == 1


def test_model_config_receives_effective_temperature(tmp_path, monkeypatch) -> None:
    from AutoGLM_GUI.config import ModelConfig

    manager, _ = _manager_for(tmp_path, monkeypatch, temperature=1)
    effective = manager.get_effective_config()

    model_config = ModelConfig(
        base_url=effective.base_url,
        api_key=effective.api_key,
        model_name=effective.model_name,
        temperature=effective.temperature,
    )

    assert model_config.temperature == 1.0
