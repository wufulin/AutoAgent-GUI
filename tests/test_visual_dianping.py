"""Visual policy contracts; no desktop, network or physical device required."""

import asyncio
import base64
import io
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageDraw

from AutoGLM_GUI.actions import ActionResult
from AutoGLM_GUI.agents.visual_dianping import (
    VisualDianpingAgent,
    protected_regions_match,
)
from AutoGLM_GUI.config import AgentConfig, ModelConfig
from AutoGLM_GUI.config_manager import (
    ConfigModel,
    SkillVisionConfig,
    UnifiedConfigManager,
)
from AutoGLM_GUI.device_protocol import FrameReference, Screenshot
from AutoGLM_GUI.devices.async_adapter import AsyncDeviceAdapter
from AutoGLM_GUI.devices.rmq1_ai import Rmq1AIAdapter
from AutoGLM_GUI.execution import resolve_route
from AutoGLM_GUI.execution_metrics import execution_metrics
from AutoGLM_GUI.execution_models import ExecutionRequest
from AutoGLM_GUI.observation import ObservationBroker, StaleObservationError
from AutoGLM_GUI.visual_dianping import (
    VisualConditionError,
    VisualDianpingController,
    VisualPage,
    parse_page,
)


def page(**overrides):
    return VisualPage.model_validate(
        dict(
            page="home",
            app="dianping",
            blocked=False,
            city="广州",
            query="",
            focused_search=False,
            keyboard="none",
            merchant_selected=False,
            shops=[],
            target=dict(role="search_field", label="搜索", x=500, y=100),
            **{},
        )
        | overrides
    )


def target(role, label="", x=500, y=100):
    return dict(role=role, label=label, x=x, y=y)


def shot(image=None, frame=None):
    if image is None:
        image = Image.new("RGB", (300, 600), "white")
        ImageDraw.Draw(image).rectangle((1, 50, 40, 80), fill="black")
    out = io.BytesIO()
    image.save(out, "PNG")
    return Screenshot(
        base64.b64encode(out.getvalue()).decode(), *image.size, frame=frame
    )


def agent():
    device = AsyncDeviceAdapter(
        SimpleNamespace(device_id="phone", get_screenshot=lambda timeout=10: shot())
    )
    original = SimpleNamespace(
        device=device,
        model_config=ModelConfig(),
        agent_config=AgentConfig(),
        cancel=AsyncMock(),
    )
    return VisualDianpingAgent(
        original, ModelConfig(model_name="executor"), "广州", "霸王茶姬"
    )


def test_exact_conditions_and_once_only_input_submit():
    c = VisualDianpingController("广州", "霸王茶姬")
    assert c.decide(page())["action"] == "Tap"
    assert (
        c.decide(page(page="city_picker", target=target("city", "广州")))["action"]
        == "Tap"
    )
    with pytest.raises(VisualConditionError):
        c.decide(page(page="city_picker", target=target("city", "广州市")))
    edit = page(page="search_input", focused_search=True)
    assert c.decide(edit)["text"] == "霸王茶姬"
    with pytest.raises(VisualConditionError):
        c.decide(edit)
    submit = page(
        page="search_input", query="霸王茶姬", target=target("submit", "搜索")
    )
    assert c.decide(submit)["action"] == "Tap"
    with pytest.raises(VisualConditionError):
        c.decide(submit)
    result = page(
        page="results",
        query="霸王茶姬",
        merchant_selected=True,
        shops=["霸王茶姬(甲店)", "霸王茶姬(乙店)"],
    )
    assert c.decide(result)["_metadata"] == "finish"
    with pytest.raises(VisualConditionError):
        c.decide(result.model_copy(update={"shops": ["霸王茶姬", "霸王茶姬"]}))


@pytest.mark.parametrize(
    "changes",
    [
        {"blocked": True},
        {"app": "unknown"},
        {"page": "unknown"},
        {"page": "city_picker", "target": target("city", "深圳")},
        {"page": "results", "city": "深圳"},
        {
            "page": "search_input",
            "focused_search": False,
            "target": target("submit", "搜索"),
        },
    ],
)
def test_unsafe_or_wrong_identity_never_executes(changes):
    with pytest.raises(VisualConditionError):
        VisualDianpingController("广州", "霸王茶姬").decide(page(**changes))


def test_ios_has_no_direct_text_or_android_back_and_requires_keyboard_focus():
    c = VisualDianpingController("广州", "霸王茶姬", ios=True)
    c.city_confirmed = True
    edit = page(
        page="search_input",
        focused_search=True,
        keyboard="zh_pinyin_full",
        target=target("keyboard_key", "b"),
    )
    assert c.decide(edit)["action"] == "Tap"
    for change in [
        {"keyboard": "other"},
        {"focused_search": False},
        {"target": target("ime_candidate", "发消息")},
        {"target": target("keyboard_key", "发送")},
    ]:
        with pytest.raises(VisualConditionError):
            c.decide(
                edit.model_copy(update=change)
                if "target" not in change
                else page(**(edit.model_dump() | change))
            )


@pytest.mark.parametrize(
    "content",
    [
        "{}",
        '{"page":"home","page":"results"}',
        "```json\n{}\n```",
        json.dumps(page().model_dump() | {"blocked": "false"}),
        json.dumps(page().model_dump() | {"extra": 1}),
        json.dumps(page().model_dump() | {"target": target("submit", x=True)}),
    ],
    ids=["missing", "duplicate", "markdown", "coercion", "extra", "bool_coordinate"],
)
def test_parser_fails_closed(content):
    with pytest.raises(ValueError):
        parse_page(content)


def test_target_change_and_header_change_require_new_decision_but_ads_do_not():
    before = shot(Image.new("RGB", (300, 600), "white"))
    action = {"action": "Tap", "element": [500, 500]}
    for rectangle, expected in [
        ((140, 285, 160, 315), False),
        ((0, 50, 200, 100), False),
        ((0, 200, 100, 240), True),
    ]:
        image = Image.new("RGB", (300, 600), "white")
        ImageDraw.Draw(image).rectangle(rectangle, fill="black")
        assert protected_regions_match(before, shot(image), action) is expected


def test_slow_inference_rechecks_once_and_never_commits_discarded_decision():
    async def run():
        a = agent()
        a.controller.city_confirmed = True
        before = await a.device.get_observation("test")
        old = replace(before, capture_started_at=before.capture_started_at - 5)
        a._perceive = AsyncMock(
            return_value=page(page="search_input", focused_search=True)
        )
        a._pending_observation = old
        a._guard = AsyncMock(return_value=None)
        events = [e async for e in a.stream("search", step_budget=1)]
        assert not a.controller.typed
        assert events[-1]["data"]["status"] == "partial"
        await a.openai_client.close()

    asyncio.run(run())


def test_fast_policy_reuses_one_capture_per_step_and_keeps_verifier_config():
    async def run():
        a = agent()
        a._perceive = AsyncMock(
            side_effect=[
                page(),
                page(
                    page="results",
                    query="霸王茶姬",
                    merchant_selected=True,
                    shops=["霸王茶姬甲", "霸王茶姬乙"],
                ),
            ]
        )
        a.action_handler.execute = AsyncMock(return_value=ActionResult(True, False))
        capture = AsyncMock(wraps=a.device.get_observation)
        a.device.get_observation = capture
        events = [e async for e in a.stream("search", step_budget=4)]
        assert capture.await_count == 2
        assert events[-1]["data"]["status"] == "completion_requested"
        assert a.model_config.model_name != "executor"
        assert a.action_handler.execute.await_count == 1
        await a.openai_client.close()

    asyncio.run(run())


def test_generation_change_during_inference_prevents_action():
    async def run():
        a = agent()

        async def perceive(observed):
            a.device.observations.invalidate()
            return page()

        a._perceive = perceive
        a.action_handler.execute = AsyncMock()
        events = [e async for e in a.stream("search")]
        assert events[-1]["data"]["status"] == "blocked"
        a.action_handler.execute.assert_not_called()
        await a.openai_client.close()

    asyncio.run(run())


@pytest.mark.parametrize("finish", ["length", "tool_calls", "content_filter"])
def test_incomplete_model_output_counted_but_never_used(finish):
    async def run():
        a = agent()
        a.openai_client.chat.completions.create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason=finish,
                        message=SimpleNamespace(content=page().model_dump_json()),
                    )
                ],
                usage=None,
            )
        )
        with execution_metrics("skill") as counts:
            with pytest.raises(VisualConditionError):
                await a._perceive(await a.device.get_observation("test"))
            assert counts.summary("blocked", "test")["executor_calls"] == 1
        await a.openai_client.close()

    asyncio.run(run())


def test_transport_geometry_invalidates_same_size_pixels():
    async def run():
        frame = FrameReference(
            "native-session", "portrait", "frame", 1920, 1080, (0.3, 0.0, 0.3, 1.0)
        )
        source = SimpleNamespace(
            get_screenshot=AsyncMock(return_value=shot(frame=frame))
        )
        broker = ObservationBroker(source)
        first = await broker.capture("one")
        assert first.screenshot.frame == frame
        source.get_screenshot.return_value = shot(
            frame=replace(frame, geometry_id="rotated")
        )
        second = await broker.capture("two")
        assert second.geometry_id != first.geometry_id and not broker.is_current(first)

    asyncio.run(run())


def test_ios_common_observation_preserves_native_guard_and_rejects_reuse():
    async def run():
        frame = FrameReference("s", "g", "f", 1920, 1080, (0.3, 0.0, 0.3, 1.0))
        raw = SimpleNamespace(device_id="rmq1:x", recovery_required=False)
        device = Rmq1AIAdapter(raw)
        device.get_screenshot = AsyncMock(return_value=shot(frame=frame))
        observed = await device.get_observation("decision")
        device.execute = AsyncMock(
            return_value={"success": True, "outcome": "completed"}
        )
        await device.execute_observation(observed, x=500, y=200)
        native = device.execute.await_args.args[0]
        assert (native.session_id, native.geometry_id, native.frame_id) == (
            "s",
            "g",
            "f",
        )
        assert device.execute.await_args.kwargs["x"] == 500
        with pytest.raises(StaleObservationError):
            await device.execute_observation(observed, x=500, y=200)

    asyncio.run(run())


def test_ios_route_only_allows_builtin_visual_revision():
    request = ExecutionRequest(
        route="skill",
        skill_id="dianping_search_shops",
        skill_version=4,
        parameters={"city": "广州", "shop_name": "霸王茶姬"},
    )
    route, _, skill = resolve_route("", request, "rmq1:x")
    assert route == "skill" and skill.builtin
    for version in [2, 3]:
        with pytest.raises(ValueError):
            resolve_route(
                "", request.model_copy(update={"skill_version": version}), "rmq1:x"
            )


def test_executor_override_is_optional_preserved_and_does_not_replace_platform_config(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(UnifiedConfigManager, "_instance", None)
    monkeypatch.setattr(UnifiedConfigManager, "_config_path", tmp_path / "config.json")
    manager = UnifiedConfigManager()
    override = SkillVisionConfig(
        base_url="https://vision.example/v1",
        model_name="visual",
        api_key="fixture-secret",
    )
    assert ConfigModel().skill_vision is None
    assert manager.save_file_config(
        "https://base.example/v1", "base", skill_vision=override, skill_vision_set=True
    )
    assert manager.get_effective_config().skill_vision == override
    assert manager.get_effective_config().model_name == "base"
    assert manager.save_file_config("https://base.example/v1", "next")
    assert manager.get_effective_config().skill_vision == override
    assert "fixture-secret" not in repr(override)
    assert manager.save_file_config(
        "https://base.example/v1", "next", skill_vision_set=True
    )
    assert manager.get_effective_config().skill_vision is None


def test_blank_frame_never_calls_model_or_authorizes_input():
    from AutoGLM_GUI.agents.visual_dianping import blank_loading_frame

    assert blank_loading_frame(shot(Image.new("RGB", (300, 600), "white")))
    assert not blank_loading_frame(shot())

    async def run():
        a = agent()
        a.device.get_screenshot = AsyncMock(
            return_value=shot(Image.new("RGB", (300, 600), "white"))
        )
        a._perceive = AsyncMock()
        a.action_handler.execute = AsyncMock()
        events = [e async for e in a.stream("search", step_budget=5)]
        assert events[-1]["data"]["status"] == "blocked"
        a._perceive.assert_not_called()
        a.action_handler.execute.assert_not_called()
        await a.openai_client.close()

    asyncio.run(run())


def test_search_start_without_observed_city_must_return_home():
    c = VisualDianpingController("广州", "霸王茶姬")
    with pytest.raises(VisualConditionError):
        c.decide(page(page="search_input", focused_search=True))
    assert (
        c.decide(
            page(
                page="search_input", focused_search=True, target=target("back", "返回")
            )
        )["action"]
        == "Tap"
    )
    assert not c.typed


def test_cancel_interrupts_inference_and_never_actions():
    async def run():
        a = agent()
        started = asyncio.Event()

        async def perceive(observed):
            started.set()
            await asyncio.Future()

        a._perceive = perceive
        a.action_handler.execute = AsyncMock()

        async def collect():
            return [e async for e in a.stream("search")]

        task = asyncio.create_task(collect())
        await started.wait()
        await a.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        a.action_handler.execute.assert_not_called()
        assert not a.is_running
        await a.openai_client.close()

    asyncio.run(run())
