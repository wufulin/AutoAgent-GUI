"""Conditioned execution observes each decision and preserves completion handoff."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from AutoGLM_GUI.agents.gemini import conditioned
from AutoGLM_GUI.agents.gemini.async_agent import AsyncGeminiAgent
from AutoGLM_GUI.config import AgentConfig, ModelConfig
from AutoGLM_GUI.device_protocol import Screenshot
from AutoGLM_GUI.execution_metrics import execution_metrics
from tests.test_dianping_controller import home, input_page, node, results, xml


class _Phone:
    device_id = "conditioned-fake-phone"

    def __init__(self, city="广州", shop="霸王茶姬"):
        self.city, self.shop = city, shop
        self.scene = "home"
        self.query = ""
        self.current_app = "大众点评"
        self.captures = 0
        self.hierarchies = 0
        self.writes = []
        self.hierarchy_override = None

    async def get_screenshot(self, timeout=10):
        self.captures += 1
        return Screenshot(f"frame-{self.captures}", 1080, 2412)

    async def get_ui_hierarchy(self, timeout=10):
        self.hierarchies += 1
        if self.hierarchy_override is not None:
            return self.hierarchy_override
        if self.scene == "home":
            return home(self.city)
        if self.scene == "input":
            return input_page(self.query)
        return results(
            query=self.query,
            merchant=self.scene == "merchants",
            cities=(self.city, self.city),
        )

    async def get_current_app(self):
        return self.current_app

    async def tap(self, x, y, delay=None):
        self.writes.append(("tap", x, y))
        if self.scene == "home":
            self.scene = "input"
        elif self.scene == "input" and self.query == self.shop:
            self.scene = "all_results"
        elif self.scene == "all_results":
            self.scene = "merchants"
        else:
            raise AssertionError("unexpected touch for the current phone state")

    async def detect_and_set_adb_keyboard(self):
        self.writes.append(("keyboard",))
        return "com.android.adbkeyboard/.AdbIME"

    async def clear_text(self):
        self.writes.append(("clear",))
        self.query = ""

    async def type_text(self, text):
        self.writes.append(("type", text))
        self.query = text

    async def launch_app(self, app):
        self.writes.append(("launch", app))
        self.current_app = app
        return True


@pytest.fixture(autouse=True)
def _no_network_or_delays(monkeypatch):
    monkeypatch.setenv("AUTOGLM_TRACE_ENABLED", "0")
    monkeypatch.setattr(
        "AutoGLM_GUI.actions.async_handler.trace_sleep_async", AsyncMock()
    )


def _agent(phone=None):
    phone = phone or _Phone()
    agent = AsyncGeminiAgent(
        ModelConfig(), AgentConfig(max_steps=10, verbose=False), phone
    )
    agent.bind_dianping(phone.city, phone.shop)
    agent.independent_verification = True
    agent._call_llm_with_tools = AsyncMock(
        side_effect=AssertionError("conditioned decision must not call a model")
    )
    agent.openai_client.chat.completions.create = AsyncMock(
        side_effect=AssertionError("no provider requests are allowed in this test")
    )
    agent.action_handler.execute = AsyncMock(wraps=agent.action_handler.execute)
    return agent, phone


async def _prime(agent):
    screenshot, app = await agent._prepare_device_state()
    agent._initial_screenshot = screenshot
    agent._prepare_initial_context("搜索目标城市品牌商户", screenshot.base64_data, app)


async def _step(agent):
    return [event async for event in agent._execute_step()][-1]["data"]


@pytest.mark.parametrize("city,shop", [("广州", "霸王茶姬"), ("深圳", "古茗")])
def test_conditioned_flow_has_one_capture_per_decision_and_zero_model_calls(
    monkeypatch, city, shop
):
    async def run():
        agent, phone = _agent(_Phone(city, shop))
        recorded = []

        async def record(phase, step, **data):
            recorded.append((phase, step, data))

        monkeypatch.setattr(conditioned, "record_phone_agent_event", record)
        with execution_metrics("skill") as metrics:
            events = [event async for event in agent.stream("查找商户")]
        steps = [event["data"] for event in events if event["type"] == "step"]
        done = events[-1]["data"]
        host_actions = [step for step in steps if step["action"]["_metadata"] == "do"]
        assert [step["action"]["action"] for step in host_actions] == [
            "Tap",
            "Type",
            "Tap",
            "Tap",
        ]
        assert phone.captures == len(host_actions) + 1 == 5
        assert phone.hierarchies == phone.captures
        assert len(agent.action_handler.execute.await_args_list) == 5
        agent._call_llm_with_tools.assert_not_awaited()
        agent.openai_client.chat.completions.create.assert_not_awaited()
        summary = metrics.summary(done["status"], done["stop_reason"])
        assert summary["executor_calls"] == 0
        assert summary["verifier_calls"] == 0
        # Executor success only requests the separate verifier; it is not a
        # verified task result, even when the UI predicates already match.
        assert done["status"] == "completion_requested"
        assert done["status"] != "succeeded"
        assert steps[-1]["action"]["_metadata"] == "finish"
        assert "独立验收" in steps[-1]["message"]
        observations = {
            step: data for phase, step, data in recorded if phase == "observation"
        }
        guards = {step: data for phase, step, data in recorded if phase == "pre_action"}
        assert len({data["observation_id"] for data in observations.values()}) == 5
        assert observations.keys() == guards.keys()
        for step, data in observations.items():
            assert guards[step]["observation_id"] == data["observation_id"]
            assert guards[step]["geometry_id"] == data["geometry_id"]
            assert "screenshot" not in guards[step]
        for phase, step, data in recorded:
            if phase == "post_action":
                assert (
                    data["observation_id"] == observations[step + 1]["observation_id"]
                )
                assert "screenshot" not in data

    asyncio.run(run())


def test_already_matching_results_only_request_independent_verification():
    async def run():
        phone = _Phone()
        phone.scene, phone.query = "merchants", phone.shop
        agent, phone = _agent(phone)
        events = [event async for event in agent.stream("查找商户")]
        assert events[-1]["data"]["status"] == "completion_requested"
        assert phone.captures == 1
        assert phone.writes == []
        agent._call_llm_with_tools.assert_not_awaited()

    asyncio.run(run())


def test_unknown_scene_stops_without_model_fallback_or_device_action():
    async def run():
        agent, phone = _agent()
        phone.hierarchy_override = xml(node(text="未知页面"))
        events = [event async for event in agent.stream("查找商户")]
        assert events[-1]["data"]["status"] == "failed"
        assert "页面未知" in events[-1]["data"]["message"]
        agent.action_handler.execute.assert_not_awaited()
        agent._call_llm_with_tools.assert_not_awaited()
        assert phone.writes == []

    asyncio.run(run())


def test_expired_initial_image_cannot_authorize_a_touch():
    async def run():
        agent, phone = _agent()
        await _prime(agent)
        assert agent._initial_observation is not None
        agent._initial_observation = replace(
            agent._initial_observation,
            capture_started_at=agent._initial_observation.capture_started_at - 2,
        )
        outcome = await _step(agent)
        assert outcome["success"] is False
        assert "屏幕观察已失效" in outcome["message"]
        agent.action_handler.execute.assert_not_awaited()
        assert phone.writes == []

    asyncio.run(run())


def test_expired_hierarchy_cannot_authorize_a_touch():
    async def run():
        agent, phone = _agent()
        await _prime(agent)
        agent._dianping_xml_started_at -= 6
        outcome = await _step(agent)
        assert outcome["success"] is False
        assert "界面节点观察已过期" in outcome["message"]
        agent.action_handler.execute.assert_not_awaited()
        assert phone.writes == []

    asyncio.run(run())


def test_foreground_change_after_first_action_stops_before_next_action():
    async def run():
        agent, phone = _agent()
        await _prime(agent)
        assert (await _step(agent))["success"]
        phone.current_app = "其他应用"
        outcome = await _step(agent)
        assert outcome["success"] is False
        assert "前台应用已改变" in outcome["message"]
        assert agent.action_handler.execute.await_count == 1
        assert len(phone.writes) == 1

    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["cancel", "pending", "unknown", "invalidate"])
def test_pre_action_boundary_prevents_dispatch(monkeypatch, boundary):
    async def run():
        agent, phone = _agent()
        await _prime(agent)

        async def record(phase, step, **data):
            if phase != "pre_action":
                return
            if boundary == "cancel":
                agent._cancel_event.set()
            elif boundary == "pending":
                agent.device._writes.add(threading.Event())
            elif boundary == "unknown":
                agent.device._outcome_unknown = True
            else:
                agent.device.observations.invalidate()

        monkeypatch.setattr(conditioned, "record_phone_agent_event", record)
        if boundary == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await _step(agent)
        else:
            assert (await _step(agent))["success"] is False
        agent.action_handler.execute.assert_not_awaited()
        assert phone.writes == []
        assert agent.device.observations.recent(100) is None

    asyncio.run(run())


def test_reset_removes_bound_controller_and_observation_session():
    async def run():
        agent, _ = _agent()
        await _prime(agent)
        session = agent.device.observations.session_id
        assert agent.dianping_controller is not None
        agent.reset()
        assert agent.dianping_controller is None
        assert agent._dianping_xml is None
        assert agent._initial_observation is None
        assert agent._last_observation is None
        assert agent.device.observations.session_id != session

    asyncio.run(run())


@pytest.mark.parametrize("changed_capture", [1, 2])
def test_hierarchy_and_image_must_not_straddle_an_observation_boundary(
    monkeypatch, changed_capture
):
    async def run():
        agent, phone = _agent()
        original_capture = agent._capture_observation
        captures = 0

        async def capture_after_external_change(purpose):
            nonlocal captures
            captures += 1
            if captures == changed_capture:
                agent.device.observations.invalidate()
            return await original_capture(purpose)

        monkeypatch.setattr(
            agent, "_capture_observation", capture_after_external_change
        )
        events = [event async for event in agent.stream("查找商户")]
        assert events[-1]["data"]["status"] == "failed"
        assert agent.action_handler.execute.await_count == changed_capture - 1
        assert len(phone.writes) == changed_capture - 1

    asyncio.run(run())


def test_foreground_change_during_pre_action_recording_prevents_dispatch(monkeypatch):
    async def run():
        agent, phone = _agent()
        await _prime(agent)

        async def record(phase, step, **data):
            if phase == "pre_action":
                phone.current_app = "其他应用"

        monkeypatch.setattr(conditioned, "record_phone_agent_event", record)
        outcome = await _step(agent)
        assert outcome["success"] is False
        agent.action_handler.execute.assert_not_awaited()
        assert phone.writes == []

    asyncio.run(run())
