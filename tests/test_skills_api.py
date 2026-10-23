"""Skill API editing contracts and storage errors."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import AutoGLM_GUI.api.skills as skills_api
from AutoGLM_GUI.skills import SkillManager

pytestmark = [pytest.mark.contract, pytest.mark.release_gate]


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    manager = SkillManager(tmp_path / "skills.json")
    monkeypatch.setattr(skills_api, "skill_manager", manager)
    app = FastAPI()
    app.include_router(skills_api.router)
    return TestClient(app)


def test_catalog_crud_revision_contract(client: TestClient):
    catalog = client.get("/api/skills").json()["skills"]
    assert len(catalog) == 6
    assert any(
        skill["id"] == "ios_wechat_open_group" and skill["platforms"] == ["ios"]
        for skill in catalog
    )
    local = {**catalog[0], "id": "my_navigation", "builtin": False}
    response = client.post("/api/skills", json=local)
    assert response.status_code == 201
    assert client.post("/api/skills", json=local).status_code == 409
    local["name"] = "Updated"
    response = client.put(
        "/api/skills/my_navigation", json={"expected_version": 1, "skill": local}
    )
    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert (
        client.put(
            "/api/skills/my_navigation", json={"expected_version": 1, "skill": local}
        ).status_code
        == 409
    )
    assert (
        client.delete("/api/skills/my_navigation?expected_version=1").status_code == 409
    )
    assert (
        client.delete("/api/skills/my_navigation?expected_version=2").status_code == 200
    )
    assert client.get("/api/skills/my_navigation").status_code == 404
    assert (
        client.delete("/api/skills/open_wechat?expected_version=1").status_code == 400
    )
    assert client.get("/api/workflows").status_code == 404


def test_validation_and_storage_errors(client: TestClient):
    assert client.post("/api/skills", json={"id": "invalid"}).status_code == 422
    skills_api.skill_manager.path.write_text("{bad")
    response = client.get("/api/skills")
    assert response.status_code == 503
    assert "repair skills.json" in response.json()["detail"]


def test_application_metadata_and_builtin_copy(client):
    response = client.get("/api/skills/dianping_search_shops")
    assert response.status_code == 200
    builtin = response.json()
    assert builtin["app"] == {"id": "dianping", "name": "大众点评"}
    assert builtin["icon"] == "search" and builtin["builtin"]
    assert builtin["version"] == 4
    copied = {
        **builtin,
        "id": "my_dianping_search",
        "version": 1,
        "builtin": False,
    }
    assert client.post("/api/skills", json=copied).status_code == 201
    assert client.get("/api/skills/my_dianping_search").json()["app"] == builtin["app"]
    assert (
        client.put(
            "/api/skills/dianping_search_shops",
            json={"expected_version": 1, "skill": builtin},
        ).status_code
        == 400
    )
    assert (
        client.delete(
            "/api/skills/dianping_search_shops?expected_version=1"
        ).status_code
        == 400
    )
