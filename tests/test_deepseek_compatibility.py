"""DeepSeek request compatibility without remote calls or device actions."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI

from AutoGLM_GUI.agents.gemini.async_agent import AsyncGeminiAgent
from AutoGLM_GUI.config import AgentConfig, ModelConfig
from AutoGLM_GUI import layered_agent_service


@pytest.mark.parametrize("model_name", ["deepseek-flash", "other-vision-model"])
def test_vision_tool_request_disables_thinking_only_for_deepseek(model_name):
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if model_name == "deepseek-flash":
            assert body["thinking"] == {"type": "disabled"}
        else:
            assert "thinking" not in body
        assert body["tool_choice"] == "required"
        assert body["custom_option"] == "preserved"
        return httpx.Response(
            200,
            json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": 0,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "Done",
                            "tool_calls": [
                                {
                                    "id": "call-finish",
                                    "type": "function",
                                    "function": {
                                        "name": "finish",
                                        "arguments": '{"message":"Done"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )

    agent = AsyncGeminiAgent(
        model_config=ModelConfig(
            model_name=model_name, extra_body={"custom_option": "preserved"}
        ),
        agent_config=AgentConfig(verbose=False),
        device=AsyncMock(),
    )

    async def run():
        async with AsyncOpenAI(
            api_key="test-key",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ) as client:
            agent.openai_client = client
            agent._prepare_initial_context("Check screen", "screen", "test.app")
            _, _, tool_name, tool_args = await agent._call_llm_with_tools()
        return tool_name, tool_args

    tool_name, tool_args = asyncio.run(run())
    assert tool_name == "finish"
    assert tool_args == {"message": "Done"}
    assert requests[0]["messages"][1]["content"][0]["type"] == "image_url"


@pytest.mark.parametrize("model_name", ["deepseek-flash", "other-planner-model"])
def test_planner_uses_compatible_thinking_setting(monkeypatch, model_name):
    monkeypatch.setattr(layered_agent_service, "get_planner_model", lambda: model_name)
    agent = layered_agent_service._create_planner_agent(AsyncMock())
    expected = (
        {"thinking": {"type": "disabled"}} if model_name == "deepseek-flash" else None
    )
    assert agent.model_settings.extra_body == expected
