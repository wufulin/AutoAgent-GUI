"""iPhone scene policy and ownership regressions; no model/network/phone required."""

from __future__ import annotations

import asyncio
import base64
import io
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageDraw

from AutoGLM_GUI.agents.rmq1.agent import (
    IPhoneAction,
    Rmq1VisionAgent,
    ScreenCheck,
    permitted,
)
from AutoGLM_GUI.config import AgentConfig, ModelConfig
from AutoGLM_GUI.config_manager import (
    ConfigModel,
    IPhoneVisionConfig,
    UnifiedConfigManager,
)
from AutoGLM_GUI.device_protocol import Screenshot
from AutoGLM_GUI.devices.rmq1_ai import (
    AIObservation,
    Rmq1AIAdapter,
    SceneChanged,
    scene_stable,
)
from AutoGLM_GUI.execution import resolve_route
from AutoGLM_GUI.execution_models import ExecutionRequest
from AutoGLM_GUI.phone_agent_manager import PhoneAgentManager
from AutoGLM_GUI.skills import SkillDefinition, skill_manager
from AutoGLM_GUI.subtasks import collect_execution


def observation(image=None):
    image = image or Image.new("RGB", (300, 600), "white")
    output = io.BytesIO()
    image.save(output, "PNG")
    screenshot = Screenshot(base64.b64encode(output.getvalue()).decode(), *image.size)
    return AIObservation(
        screenshot,
        image,
        "s" * 32,
        "portrait",
        "f" * 32,
        1920,
        1080,
        {"x": 0.36, "y": 0.0, "width": 0.28, "height": 1.0},
    )


def check(**kwargs):
    return ScreenCheck.model_validate(
        {
            "allowed": True,
            "page": "wechat_search",
            "ready": True,
            "reason": "fixture",
            "search_text": "青城",
            "group_name": "青城",
            "category": "group",
            "exact_match_count": 1,
            "results_complete": True,
            "target_label": "青城",
            **kwargs,
        }
    )


def test_crop_mapping_and_geometry_binding():
    before = observation()
    assert before.point(500, 500) == pytest.approx((0.5, 0.5))
    assert before.point(0, 1000) == pytest.approx((0.36, 1))
    for value in (-1, 1001, 1.5, True):
        with pytest.raises(ValueError):
            before.point(value, 500)
    assert not scene_stable(before, replace(before, geometry_id="landscape"), [])
    assert not scene_stable(before, replace(before, session_id="new"), [])


def test_scene_guard_tolerates_clock_and_codec_noise_but_rejects_target_and_overlay():
    before = observation()
    image = before.image.copy()
    ImageDraw.Draw(image).rectangle((0, 0, 60, 15), fill="black")
    assert scene_stable(before, observation(image), [(500, 500)])
    assert scene_stable(
        before, observation(Image.new("RGB", (300, 600), (253, 253, 253))), [(500, 500)]
    )
    image = before.image.copy()
    ImageDraw.Draw(image).rectangle((142, 292, 158, 308), fill="black")
    assert not scene_stable(before, observation(image), [(500, 500)])
    image = before.image.copy()
    ImageDraw.Draw(image).rectangle((0, 70, 299, 130), fill="gray")
    assert not scene_stable(before, observation(image), [(500, 500)])


@pytest.mark.parametrize(
    "changes",
    [
        {"search_text": "倾城"},
        {"group_name": "青城山"},
        {"category": "contact"},
        {"exact_match_count": 2},
        {"exact_match_count": 0},
        {"results_complete": False},
        {"allowed": False},
        {"page": "unknown"},
        {"target_label": "倾城"},
    ],
)
def test_group_entry_requires_exact_unique_group_and_full_results(changes):
    action = IPhoneAction(action="tap", page="wechat_search", target="group_result")
    assert not permitted(action, check(**changes), "enter", "青城", True, False)


def test_group_entry_requires_previous_stage_and_final_completion_requires_own_entry():
    action = IPhoneAction(action="tap", page="wechat_search", target="group_result")
    assert not permitted(action, check(), "enter", "青城", False, False)
    assert permitted(action, check(), "enter", "青城", True, False)
    final = IPhoneAction(action="finish", page="group_chat", target="none")
    page = check(page="group_chat")
    assert not permitted(final, page, "enter", "青城", True, False)
    assert permitted(final, page, "enter", "青城", True, True)


@pytest.mark.parametrize("phase", ["open", "search", "enter"])
@pytest.mark.parametrize("kind", ["tap", "swipe", "wait"])
def test_group_page_never_allows_input_or_history_scroll(phase, kind):
    action = IPhoneAction(action=kind, page="group_chat", target="keyboard_key")
    assert not permitted(
        action,
        check(page="group_chat", focused_search=True, keyboard="zh_pinyin_full"),
        phase,
        "青城",
        True,
        True,
    )


@pytest.mark.parametrize(
    "page,focused,keyboard",
    [
        ("wechat_home", True, "zh_pinyin_full"),
        ("wechat_search", False, "zh_pinyin_full"),
        ("wechat_search", True, "unknown"),
        ("group_chat", True, "zh_pinyin_full"),
    ],
)
def test_input_requires_supported_keyboard_and_search_focus(page, focused, keyboard):
    action = IPhoneAction(action="tap", page=page, target="keyboard_key")
    assert not permitted(
        action,
        check(page=page, focused_search=focused, keyboard=keyboard),
        "search",
        "青城",
        False,
        False,
    )


def test_skill_platform_defaults_and_iphone_route_are_enforced():
    ios = skill_manager.compile_skill(
        "ios_wechat_open_group", 1, {"group_name": "青城"}
    )
    assert ios.platforms == ("ios",)
    android = skill_manager.get_skill("open_wechat")
    assert android and android.platforms == ["android"]
    old = android.model_dump(exclude={"platforms"})
    assert SkillDefinition.model_validate(old).platforms == ["android"]
    request = ExecutionRequest(route="auto", max_seconds=600)
    for message in [
        "搜索并进入“青城”群，不发送消息",
        "微信中搜索并进入群聊“青城”",
        "搜索并进入青城群",
    ]:
        route, _, skill = resolve_route(message, request, "rmq1:192.168.1.20")
        assert route == "skill" and skill and skill.parameters["group_name"] == "青城"
    for message in [
        "给青城发消息",
        "搜索并进入青城群，然后发送消息",
        "打开微信",
        "搜索并进入青城群，删除记录",
    ]:
        with pytest.raises(ValueError):
            resolve_route(message, request, "rmq1:192.168.1.20")
    with pytest.raises(ValueError):
        resolve_route(
            "ignored",
            ExecutionRequest(route="skill", skill_id="open_wechat", skill_version=1),
            "rmq1:192.168.1.20",
        )


def test_iphone_config_is_optional_separate_and_validated(tmp_path, monkeypatch):
    cfg = ConfigModel()
    assert cfg.iphone_vision is None
    monkeypatch.setattr(UnifiedConfigManager, "_instance", None)
    monkeypatch.setattr(UnifiedConfigManager, "_config_path", tmp_path / "config.json")
    manager = UnifiedConfigManager()
    iphone = IPhoneVisionConfig(
        base_url="https://vision.example/v1/",
        model_name="ios-model",
        api_key="fixture-private",
    )
    assert manager.save_file_config(
        "http://android.example/v1",
        "android-model",
        iphone_vision=iphone,
        iphone_vision_set=True,
    )
    assert manager.get_effective_config().iphone_vision == iphone
    assert manager.save_file_config("http://android.example/v1", "android-next")
    assert manager.get_effective_config().iphone_vision == iphone
    assert manager.save_file_config(
        "http://android.example/v1", "android-next", iphone_vision_set=True
    )
    assert manager.get_effective_config().iphone_vision is None
    with pytest.raises(ValueError):
        IPhoneVisionConfig(base_url="file:///tmp", model_name="model")
    assert "fixture-private" not in repr(iphone)


def test_late_websocket_result_cannot_release_or_quarantine_another_owner(monkeypatch):
    async def run():
        manager = PhoneAgentManager()
        monkeypatch.setattr(manager, "_physical_key", lambda key: key)
        await manager.acquire_manual_control("rmq1:x", "new")
        await manager.settle_rmq1_manual("rmq1:x", "old", False)
        await manager.settle_rmq1_manual("rmq1:x", "old", True)
        assert manager._manual_owners["rmq1:x"] == "new"
        assert "rmq1:x" not in manager._manual_uncertain
        await manager.settle_rmq1_manual("rmq1:x", "new", False)
        await manager.acquire_device_lease("rmq1:x", "task")
        with pytest.raises(Exception, match="AI|owns"):
            await manager.acquire_manual_control("rmq1:x", "manual")

    asyncio.run(run())


def test_adapter_observation_is_independent_of_preview_eviction(monkeypatch):
    async def run():
        image = Image.new("RGB", (1920, 1080), "black")
        ImageDraw.Draw(image).rectangle((690, 0, 1230, 1079), fill="white")
        output = io.BytesIO()
        image.save(output, "PNG")
        payload = {
            "image": base64.b64encode(output.getvalue()).decode(),
            "content_rect": {"x": 690 / 1920, "y": 0, "width": 540 / 1920, "height": 1},
            "session_id": "s",
            "geometry_id": "g",
            "frame_id": "old",
        }
        raw = SimpleNamespace(
            device_id="rmq1:x", observe=lambda: payload, recovery_required=False
        )
        adapter = Rmq1AIAdapter(raw)
        before = await adapter.observe()
        payload["frame_id"] = "new"  # Simulates the preview frame table rolling over.
        after = await adapter.observe()
        assert before.frame_id == "old" and after.frame_id == "new"
        assert before.screenshot.width == 540 and before.screenshot.height == 1080
        assert before.image.getpixel((200, 200)) == (255, 255, 255)
        assert scene_stable(before, after, [(500, 500)])

    asyncio.run(run())


def test_adapter_revalidates_model_observation_before_dispatch(monkeypatch):
    async def run():
        adapter = Rmq1AIAdapter(
            SimpleNamespace(device_id="rmq1:x", recovery_required=False)
        )
        adapter.validate_owner = AsyncMock()
        before = observation()
        adapter.observe = AsyncMock(return_value=replace(before, geometry_id="changed"))
        monkeypatch.setattr(
            adapter, "_perform", lambda *args: pytest.fail("must not dispatch")
        )
        with pytest.raises(SceneChanged):
            await adapter.execute(before, x=500, y=500)
        assert not adapter.pending_writes
        # A slow model is permitted only with a newly observed matching scene.
        adapter.observe = AsyncMock(return_value=replace(before, frame_id="fresh"))
        calls = []
        monkeypatch.setattr(
            adapter,
            "_perform",
            lambda frame, *args: calls.append(frame.frame_id)
            or {"success": True, "outcome": "completed"},
        )
        assert (await adapter.execute(before, x=500, y=500))["success"]
        assert calls == ["fresh"]

    asyncio.run(run())


def test_three_changed_scenes_stop_without_input_and_preserve_needs_user(monkeypatch):
    async def run():
        adapter = SimpleNamespace(
            lease_owner="task",
            pending_writes=False,
            outcome_unknown=False,
            validate_owner=AsyncMock(),
            observe=AsyncMock(return_value=observation()),
            cancel_input=lambda: None,
        )
        adapter.execute = AsyncMock(side_effect=SceneChanged("changed"))
        agent = Rmq1VisionAgent(ModelConfig(), AgentConfig(), adapter)
        agent.bind_scenario("青城", "search", False)
        agent._propose = AsyncMock(
            return_value=IPhoneAction(
                action="tap", page="wechat_search", target="search_field"
            )
        )
        agent._inspect = AsyncMock(return_value=check())
        result = await collect_execution(agent, "search", None, 20)
        assert result.status == "needs_user"
        assert adapter.execute.await_count == 3
        await agent.openai_client.close()

    asyncio.run(run())


def test_group_finish_cannot_be_reported_before_confirmed_entry():
    async def run():
        adapter = SimpleNamespace(
            lease_owner="task",
            pending_writes=False,
            outcome_unknown=False,
            validate_owner=AsyncMock(),
            observe=AsyncMock(return_value=observation()),
            cancel_input=lambda: None,
        )
        agent = Rmq1VisionAgent(ModelConfig(), AgentConfig(), adapter)
        agent.bind_scenario("青城", "enter", True)
        agent._propose = AsyncMock(
            return_value=IPhoneAction(action="finish", page="group_chat", target="none")
        )
        agent._inspect = AsyncMock(return_value=check(page="group_chat"))
        result = await collect_execution(agent, "enter", None, 3)
        assert result.status == "needs_user"
        await agent.openai_client.close()

    asyncio.run(run())


@pytest.mark.parametrize("unknown", [False, True])
def test_cancelled_adapter_retains_inflight_write_until_it_settles(
    monkeypatch, unknown
):
    import threading

    async def run():
        adapter = Rmq1AIAdapter(
            SimpleNamespace(device_id="rmq1:x", recovery_required=False)
        )
        adapter.validate_owner = AsyncMock()
        adapter.observe = AsyncMock(return_value=observation())
        started, release = threading.Event(), threading.Event()

        def perform(*args):
            started.set()
            release.wait(2)
            return {
                "success": not unknown,
                "outcome": "unknown" if unknown else "completed",
                "cancelled": True,
            }

        monkeypatch.setattr(adapter, "_perform", perform)
        task = asyncio.create_task(adapter.execute(observation(), x=500, y=500))
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert adapter.pending_writes
        assert not await adapter.drain(0.01)
        release.set()
        assert await adapter.drain(2) is (not unknown)
        assert not adapter.pending_writes
        assert adapter.outcome_unknown is unknown

    asyncio.run(run())


@pytest.mark.parametrize("final_verdict", ["met", "unmet"])
def test_three_phase_skill_uses_shared_verifier_and_stops_after_group_entry(
    monkeypatch, final_verdict
):
    from AutoGLM_GUI.execution import execute_skill
    from AutoGLM_GUI.subtasks import SubtaskRunContext, Verification

    async def run():
        writes = []
        adapter = SimpleNamespace(
            lease_owner="layered:task",
            pending_writes=False,
            outcome_unknown=False,
            validate_owner=AsyncMock(),
            observe=AsyncMock(return_value=observation()),
            cancel_input=lambda: None,
        )
        agent = Rmq1VisionAgent(ModelConfig(), AgentConfig(), adapter)
        phases = set()

        async def propose(observed, task):
            phase = agent._phase
            if phase in phases:
                return IPhoneAction(
                    action="finish",
                    page={
                        "open": "wechat_home",
                        "search": "wechat_search",
                        "enter": "group_chat",
                    }[phase],
                    target="none",
                )
            phases.add(phase)
            return IPhoneAction(
                action="tap",
                page={
                    "open": "home",
                    "search": "wechat_home",
                    "enter": "wechat_search",
                }[phase],
                target={
                    "open": "wechat_icon",
                    "search": "search_field",
                    "enter": "group_result",
                }[phase],
            )

        async def inspect(observed, action):
            if action.page == "home":
                return check(page="home", target_label="微信")
            return check(page=action.page)

        async def execute(*args, **kwargs):
            writes.append(agent._phase)
            return {
                "success": True,
                "outcome": "completed",
                "receipt_scope": "kvm_transport",
            }

        adapter.execute = execute
        agent._propose, agent._inspect = propose, inspect
        manager = SimpleNamespace(
            acquire_device_lease=AsyncMock(),
            validate_device_lease=AsyncMock(),
            get_agent_with_context_async=AsyncMock(return_value=agent),
            quarantine_device=AsyncMock(),
        )
        monkeypatch.setattr(PhoneAgentManager, "get_instance", lambda: manager)
        monkeypatch.setattr(
            SubtaskRunContext, "fingerprint", staticmethod(lambda: "config")
        )
        context = SubtaskRunContext("task", "session", "rmq1:x", 60, "config")
        verified = []

        async def verify(device, model, goal, criteria):
            verified.append(agent._phase)
            verdict = final_verdict if agent._phase == "enter" else "met"
            return Verification(
                criteria=[
                    {
                        "criterion": item,
                        "verdict": verdict,
                        "reason": "fixture fresh frame",
                    }
                    for item in criteria
                ],
                facts=["visible"],
                observed_at="2026-10-21T00:00:00Z",
                screenshot_sha256="fixture",
            )

        context._verify = verify
        skill = skill_manager.compile_skill(
            "ios_wechat_open_group", 1, {"group_name": "青城"}
        )
        result = await execute_skill(context, "rmq1:x", skill)
        assert result.success is (final_verdict == "met")
        assert context.final_result()[0] is (final_verdict == "met")
        assert verified == ["open", "search", "enter"]
        assert writes == ["open", "search", "enter"]
        assert context.ios_search_verified
        await agent.openai_client.close()

    asyncio.run(run())


def test_direct_subtask_cannot_bypass_iphone_skill(monkeypatch):
    from AutoGLM_GUI.subtasks import SubtaskRunContext

    async def run():
        monkeypatch.setattr(
            SubtaskRunContext, "fingerprint", staticmethod(lambda: "config")
        )
        manager = SimpleNamespace(acquire_device_lease=AsyncMock())
        monkeypatch.setattr(PhoneAgentManager, "get_instance", lambda: manager)
        context = SubtaskRunContext("task", "session", "rmq1:x", 60, "config")
        result = await context.execute("rmq1:x", "send a message", ["sent"], [], 5)
        assert not result.success and result.stop_reason == "iphone_skill_required"
        manager.acquire_device_lease.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize("valid", [True, False])
def test_capability_probe_uses_generated_image_tools_then_readonly_json(
    monkeypatch, valid
):
    import json
    import openai
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from AutoGLM_GUI.api.agents import router

    calls = []

    class Client:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def create(self, **kwargs):
            calls.append(kwargs)
            if "tools" in kwargs:
                message = SimpleNamespace(
                    tool_calls=[
                        SimpleNamespace(
                            type="function",
                            function=SimpleNamespace(
                                name="report_color",
                                arguments=json.dumps(
                                    {"color": "red" if valid else "blue"}
                                ),
                            ),
                        )
                    ]
                )
            else:
                message = SimpleNamespace(content='{"color":"red"}')
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(openai, "AsyncOpenAI", Client)
    monkeypatch.setattr(
        PhoneAgentManager,
        "get_instance",
        lambda: pytest.fail("probe must not access a phone"),
    )
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).post(
        "/api/config/iphone-vision-check",
        json={
            "base_url": "https://fixture.example/v1",
            "model_name": "fixture",
            "api_key": "private-fixture",
        },
    )
    assert response.status_code == 200 and response.json()["success"] is valid
    assert "private-fixture" not in response.text
    assert len(calls) == (2 if valid else 1)
    url = next(
        item["image_url"]["url"]
        for item in calls[0]["messages"][0]["content"]
        if item["type"] == "image_url"
    )
    picture = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert picture.size == (64, 64) and picture.getpixel((32, 32)) == (255, 0, 0)
    if valid:
        assert "tools" not in calls[1]


def test_recovery_does_not_clear_marker_while_ai_holds_lease(monkeypatch):
    from AutoGLM_GUI.device_manager import DeviceManager
    from unittest.mock import Mock

    async def run():
        manager = PhoneAgentManager()
        monkeypatch.setattr(manager, "_physical_key", lambda value: value)
        raw = SimpleNamespace(recover=Mock())
        monkeypatch.setattr(
            DeviceManager,
            "get_instance",
            lambda: SimpleNamespace(get_rmq1_device=lambda _: raw),
        )
        await manager.acquire_device_lease("rmq1:x", "task")
        with pytest.raises(Exception, match="settlement"):
            await manager.recover_rmq1_control("rmq1:x", "s" * 32)
        raw.recover.assert_not_called()
        await manager.release_device_lease("rmq1:x", "task")
        await manager.recover_rmq1_control("rmq1:x", "s" * 32)
        raw.recover.assert_called_once()

    asyncio.run(run())
