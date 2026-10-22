"""Read-only completion verification fails closed with safe diagnostics."""

import asyncio
import base64
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError

from AutoGLM_GUI import subtasks
from AutoGLM_GUI.config import ModelConfig
from AutoGLM_GUI.device_protocol import Screenshot
from AutoGLM_GUI.trace import trace_context, trace_span

FRAME = base64.b64encode(b"fresh-frame-private-content").decode()
CRITERIA = ["title", "list"]


def reply_content(criteria=CRITERIA, verdict="met", **extra):
    return json.dumps(
        {
            "criteria": [
                {"criterion": item, "verdict": verdict, "reason": "visible evidence"}
                for item in criteria
            ],
            "facts": ["visible list"],
            **extra,
        }
    )


def response(content=None, *, finish_reason="stop", usage=None, choices=None):
    return SimpleNamespace(
        choices=(
            [
                SimpleNamespace(
                    finish_reason=finish_reason,
                    message=SimpleNamespace(
                        content=content,
                        reasoning_content="private-chain-of-thought",
                    ),
                )
            ]
            if choices is None
            else choices
        ),
        usage=usage,
        provider_secret="private-response-metadata",
        _request_id="private-provider-request-id",
    )


@pytest.fixture
def harness(monkeypatch, tmp_path):
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv("AUTOGLM_TRACE_FILE", str(trace_file))
    monkeypatch.setenv("AUTOGLM_TRACE_ENABLED", "1")
    create = AsyncMock(return_value=response(reply_content()))
    screenshot = Screenshot(FRAME, 10, 10)

    async def capture():
        with trace_span("test.capture"):
            return screenshot

    device = SimpleNamespace(get_screenshot=AsyncMock(side_effect=capture))
    model = ModelConfig(
        base_url="https://example.test/v1",
        api_key="private-model-key",
        model_name="vision-test",
        temperature=0.2,
    )
    clients = []
    closed = []

    class Client:
        def __init__(self, **kwargs):
            clients.append(kwargs)

        async def __aenter__(self):
            return SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=create))
            )

        async def __aexit__(self, *args):
            closed.append(self)

    monkeypatch.setattr(subtasks, "AsyncOpenAI", Client)

    async def run(criteria=CRITERIA):
        with trace_context("test-verification"):
            return await subtasks.verify_completion(device, model, "goal", criteria)

    def records():
        return [json.loads(line) for line in trace_file.read_text().splitlines()]

    def assert_error_trace(error):
        verification_span = next(
            item for item in records() if item["name"] == "subtask.verify"
        )
        assert verification_span["attrs"]["error_code"] == error

    def assert_private_data_absent(result, *additional):
        serialized = result.model_dump_json() + trace_file.read_text()
        for value in (
            FRAME,
            "fresh-frame-private-content",
            "private-model-key",
            "private-chain-of-thought",
            "private-response-metadata",
            "private-provider-request-id",
            *additional,
        ):
            assert value not in serialized

    return SimpleNamespace(
        run=run,
        create=create,
        device=device,
        model=model,
        clients=clients,
        closed=closed,
        records=records,
        trace_file=trace_file,
        assert_private_data_absent=assert_private_data_absent,
        assert_error_trace=assert_error_trace,
    )


@pytest.mark.parametrize("verdict", ["met", "unmet", "unknown"])
def test_verifier_uses_fresh_image_without_tools_and_preserves_verdict(
    harness, verdict
):
    harness.create.return_value = response(reply_content(verdict=verdict))
    result = asyncio.run(harness.run())
    harness.device.get_screenshot.assert_awaited_once()
    harness.create.assert_awaited_once()
    assert len(harness.clients) == 1
    assert len(harness.closed) == 1
    assert harness.clients[0]["max_retries"] == 0
    assert harness.clients[0]["timeout"] == 120
    assert subtasks.VERIFICATION_TIMEOUT_SECONDS > harness.clients[0]["timeout"]
    request = harness.create.call_args.kwargs
    assert request["max_tokens"] == 4096
    assert request["model"] == harness.model.model_name
    assert request["temperature"] == harness.model.temperature
    assert "tools" not in request and "functions" not in request
    assert [item["role"] for item in request["messages"]] == ["system", "user"]
    assert request["messages"][1]["content"][0]["image_url"]["url"].endswith(FRAME)
    assert (
        result.screenshot_sha256 == hashlib.sha256(base64.b64decode(FRAME)).hexdigest()
    )
    assert [item.verdict for item in result.criteria] == [verdict] * 2
    assert result.error is None
    assert result.diagnostics.stage == "criteria"
    harness.assert_private_data_absent(result)
    spans = {item["name"]: item for item in harness.records()}
    assert spans["model.call"]["parent_span_id"] == spans["subtask.verify"]["span_id"]
    assert spans["test.capture"]["parent_span_id"] == spans["subtask.verify"]["span_id"]


def test_provider_extensions_cannot_override_read_only_request(harness):
    reserved = {
        "model": "other-model",
        "messages": [{"role": "user", "content": "private-injected-message"}],
        "temperature": 9,
        "max_tokens": 1500,
        "max_completion_tokens": 1,
        "stream": True,
        "tools": [{"type": "function", "function": {"name": "tap"}}],
        "tool_choice": "required",
        "functions": [{"name": "tap"}],
        "function_call": {"name": "tap"},
        "n": 2,
        "response_format": {"type": "text"},
    }
    provider_extensions = {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
    harness.model.extra_body = {**reserved, **provider_extensions}
    result = asyncio.run(harness.run())
    assert result.error is None
    request = harness.create.call_args.kwargs
    assert request["max_tokens"] == 4096
    assert request["model"] == "vision-test"
    assert request["extra_body"] == provider_extensions
    assert harness.model.extra_body == {**reserved, **provider_extensions}
    assert [item["role"] for item in request["messages"]] == ["system", "user"]
    assert "tools" not in request and "functions" not in request
    harness.assert_private_data_absent(result, "private-injected-message")


@pytest.mark.parametrize("thinking", [None, "enabled", "disabled"])
def test_deepseek_verification_preserves_provider_thinking_configuration(
    harness, thinking
):
    harness.model.model_name = "deepseek-flash"
    extensions = {
        "reasoning_effort": "low",
    }
    if thinking is not None:
        extensions["thinking"] = {"type": thinking}
    harness.model.extra_body = {**extensions, "tools": [{"type": "function"}]}
    result = asyncio.run(harness.run())
    assert result.error is None
    assert harness.create.call_args.kwargs["extra_body"] == extensions
    assert harness.model.extra_body == {
        **extensions,
        "tools": [{"type": "function"}],
    }


@pytest.mark.parametrize(
    "criteria,error",
    [
        (["list", "title"], "verification_criteria_mismatch"),
        (["title", "title"], "verification_criteria_mismatch"),
        (["title"], "verification_criteria_mismatch"),
        (["title", "list", "extra"], "verification_criteria_mismatch"),
        (["title", "unknown"], "verification_criteria_mismatch"),
        (["Title", "list"], "verification_criteria_mismatch"),
        ([1, "list"], "verification_schema_invalid"),
        ([True, "list"], "verification_schema_invalid"),
        ([None, "list"], "verification_schema_invalid"),
    ],
)
def test_full_criteria_reject_missing_reordered_duplicate_or_nonstring_conditions(
    harness, criteria, error
):
    harness.create.return_value = response(reply_content(criteria))
    result = asyncio.run(harness.run())
    assert result.error == error
    assert result.criteria == []
    harness.create.assert_awaited_once()


@pytest.mark.parametrize(
    "content,error",
    [
        (
            '{"criteria":[],"criteria":[],"facts":[]}',
            "verification_json_invalid",
        ),
        (
            '{"criteria":[{"criterion":"title","criterion":"list","verdict":"met","reason":"seen"}],"facts":[]}',
            "verification_json_invalid",
        ),
        ('{"criteria":[],"facts":[NaN]}', "verification_json_invalid"),
    ],
)
def test_ambiguous_or_nonstandard_json_cannot_hide_conditions(harness, content, error):
    harness.create.return_value = response(content)
    result = asyncio.run(harness.run())
    assert result.error == error
    assert result.criteria == []


@pytest.mark.parametrize(
    "content", ['{"criteria":[{"criterion":"title"', reply_content()]
)
def test_length_response_is_rejected_even_with_valid_json(harness, content):
    harness.create.return_value = response(
        content,
        finish_reason="length",
        usage=SimpleNamespace(
            prompt_tokens=4210,
            completion_tokens=1500,
            total_tokens=5710,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=1229),
        ),
    )
    result = asyncio.run(harness.run())
    assert result.error == "verification_output_truncated"
    assert result.criteria == [] and result.facts == []
    assert result.diagnostics.stage == "response"
    harness.assert_error_trace(result.error)
    harness.create.assert_awaited_once()
    diagnostics = result.diagnostics.model_dump(exclude_none=True)
    assert diagnostics["finish_reason"] == "length"
    assert diagnostics["completion_tokens"] == 1500
    assert diagnostics["reasoning_tokens"] == 1229
    assert diagnostics["prompt_tokens"] == 4210
    assert diagnostics["total_tokens"] == 5710
    spans = {item["name"]: item for item in harness.records()}
    assert "verification_output_truncated" in json.dumps(spans["subtask.verify"])
    assert "1229" in json.dumps(spans["subtask.verify"])
    harness.assert_private_data_absent(result)


def test_diagnostics_retain_only_response_summary_and_safe_usage(harness):
    content = reply_content()
    harness.create.return_value = response(
        content,
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=40,
            total_tokens=140,
            completion_tokens_details=SimpleNamespace(
                reasoning_tokens=10,
                reasoning_content="private-usage-thoughts",
            ),
            provider_field="private-usage-field",
        ),
    )
    result = asyncio.run(harness.run())
    diagnostics = result.diagnostics.model_dump(exclude_none=True)
    assert diagnostics["max_tokens"] == 4096
    assert diagnostics["response_chars"] == len(content)
    assert (
        diagnostics["response_sha256"] == hashlib.sha256(content.encode()).hexdigest()
    )
    assert (
        diagnostics["request_id_sha256"]
        == hashlib.sha256(b"private-provider-request-id").hexdigest()
    )
    assert diagnostics["completion_tokens"] == 40
    assert diagnostics["reasoning_tokens"] == 10
    harness.assert_private_data_absent(
        result, "private-usage-thoughts", "private-usage-field"
    )


@pytest.mark.parametrize(
    "finish_reason", ["tool_calls", "content_filter", None, "private-finish-value"]
)
def test_non_stop_finish_is_incomplete(harness, finish_reason):
    harness.create.return_value = response(reply_content(), finish_reason=finish_reason)
    result = asyncio.run(harness.run())
    assert result.error == "verification_response_incomplete"
    assert result.diagnostics.stage == "response"
    harness.assert_error_trace(result.error)
    assert result.criteria == []
    harness.create.assert_awaited_once()
    harness.assert_private_data_absent(result, "private-finish-value")


@pytest.mark.parametrize(
    "content,choices", [(None, []), (None, None), ("", None), ("  ", None)]
)
def test_empty_response_has_distinct_error(harness, content, choices):
    harness.create.return_value = response(content, choices=choices)
    result = asyncio.run(harness.run())
    assert result.error == "verification_response_empty"
    assert result.diagnostics.stage == "response"
    harness.assert_error_trace(result.error)
    assert result.criteria == []
    harness.create.assert_awaited_once()
    harness.assert_private_data_absent(result)


@pytest.mark.parametrize(
    "content,error",
    [
        ("private-invalid-json", "verification_json_invalid"),
        ('{"criteria":', "verification_json_invalid"),
        ('{"criteria":[],"facts":[]}', "verification_criteria_mismatch"),
        (reply_content(["list", "title"]), "verification_criteria_mismatch"),
        (reply_content(["Title", "list"]), "verification_criteria_mismatch"),
        (reply_content(["title"]), "verification_criteria_mismatch"),
        ('{"criteria":"private-schema-value"}', "verification_schema_invalid"),
        ("[]", "verification_schema_invalid"),
    ],
)
def test_invalid_output_is_classified_without_raw_validation_details(
    harness, content, error
):
    harness.create.return_value = response(content)
    result = asyncio.run(harness.run())
    assert result.error == error
    assert result.diagnostics.stage == (
        "criteria" if error == "verification_criteria_mismatch" else "parse"
    )
    harness.assert_error_trace(error)
    assert result.criteria == [] and result.facts == []
    harness.create.assert_awaited_once()
    assert error in json.dumps(harness.records())
    harness.assert_private_data_absent(
        result,
        "private-invalid-json",
        "private-schema-value",
        "private-invalid-verdict",
        "private-extra-field",
        "private-extra-value",
    )


@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```", "```\n{}\n```"])
def test_extra_fields_including_nested_facts_do_not_block_verification(
    harness, wrapper
):
    payload = json.loads(reply_content())
    payload["private-extra-field"] = "private-extra-value"
    payload["verdict"] = "unmet"  # Only per-condition judgments are authoritative.
    for item in payload["criteria"]:
        item["facts"] = ["private-nested-fact"]
        item["private-extra-field"] = "private-extra-value"
    harness.create.return_value = response(wrapper.format(json.dumps(payload)))
    result = asyncio.run(harness.run())
    assert result.error is None
    assert [item.verdict for item in result.criteria] == ["met", "met"]
    assert result.facts == ["visible list"]
    harness.device.get_screenshot.assert_awaited_once()
    harness.create.assert_awaited_once()
    harness.assert_private_data_absent(
        result, "private-extra-field", "private-extra-value", "private-nested-fact"
    )


@pytest.mark.parametrize(
    "facts,expected",
    [
        (None, []),
        ("visible list", ["visible list"]),
        (42, []),
        ({"text": "ignored"}, []),
        (["visible list", None, 3, True, "", "  "], ["visible list"]),
        ([f"fact {i}" for i in range(21)], [f"fact {i}" for i in range(20)]),
    ],
)
def test_optional_facts_are_normalized_without_changing_judgments(
    harness, facts, expected
):
    payload = json.loads(reply_content())
    payload["facts"] = facts
    harness.create.return_value = response(json.dumps(payload))
    result = asyncio.run(harness.run())
    assert result.error is None
    assert result.facts == expected
    assert all(item.verdict == "met" for item in result.criteria)


def test_single_condition_object_and_missing_facts_are_accepted(harness):
    harness.create.return_value = response(
        json.dumps(
            {"criteria": {"criterion": "title", "verdict": " MET ", "reason": "seen"}}
        )
    )
    result = asyncio.run(harness.run(criteria=["title"]))
    assert result.error is None
    assert result.criteria[0].verdict == "met"
    assert result.facts == []


@pytest.mark.parametrize("field", ["verdict", "reason"])
@pytest.mark.parametrize("value", ["missing", None, "", "  ", True, 1, [], {}])
def test_missing_or_invalid_judgment_evidence_is_unknown(harness, field, value):
    payload = json.loads(reply_content())
    payload["criteria"][0][field] = value
    if value == "missing":
        del payload["criteria"][0][field]
    harness.create.return_value = response(json.dumps(payload))
    result = asyncio.run(harness.run())
    assert result.error is None
    assert result.criteria[0].criterion == "title"
    assert result.criteria[0].verdict == "unknown"
    assert result.criteria[1].verdict == "met"
    harness.create.assert_awaited_once()


def test_unrecognized_verdict_is_unknown_instead_of_inferred_from_other_fields(harness):
    harness.create.return_value = response(
        reply_content(verdict="private-invalid-verdict", verdict_summary="met")
    )
    result = asyncio.run(harness.run())
    assert result.error is None
    assert all(item.verdict == "unknown" for item in result.criteria)
    harness.assert_private_data_absent(result, "private-invalid-verdict")


@pytest.mark.parametrize("verdict", ["unmet", "unknown"])
def test_extra_fields_cannot_override_negative_or_unknown_judgment(harness, verdict):
    payload = json.loads(reply_content(verdict=verdict))
    payload["verdict"] = "met"
    payload["success"] = True
    for item in payload["criteria"]:
        item["facts"] = ["looks successful"]
        item["success"] = True
    harness.create.return_value = response(json.dumps(payload))
    result = asyncio.run(harness.run())
    assert result.error is None
    assert [item.verdict for item in result.criteria] == [verdict, verdict]


@pytest.mark.parametrize("timeout", [False, True])
def test_request_errors_are_safe_and_never_retried(harness, timeout):
    request = httpx.Request("POST", "https://example.test/private-provider-url")
    harness.create.side_effect = (
        APITimeoutError(request=request)
        if timeout
        else APIConnectionError(message="private-provider-error", request=request)
    )
    result = asyncio.run(harness.run())
    expected = "verification_timeout" if timeout else "verification_request_failed"
    assert result.error == expected
    assert result.diagnostics.stage == "request"
    harness.assert_error_trace(expected)
    assert result.criteria == []
    harness.create.assert_awaited_once()
    assert expected in json.dumps(harness.records())
    harness.assert_private_data_absent(
        result, "private-provider-error", "private-provider-url"
    )


@pytest.mark.parametrize(
    "kind", ["capture_error", "missing", "sensitive", "invalid_base64"]
)
def test_unusable_screenshot_never_calls_model(harness, kind):
    if kind == "capture_error":
        harness.device.get_screenshot.side_effect = OSError("private-device-error")
    else:
        harness.device.get_screenshot.side_effect = None
        harness.device.get_screenshot.return_value = SimpleNamespace(
            base64_data=""
            if kind == "missing"
            else ("!invalid!" if kind == "invalid_base64" else FRAME),
            is_sensitive=kind == "sensitive",
        )
    result = asyncio.run(harness.run())
    assert result.error == "verification_screenshot_unavailable"
    assert result.diagnostics.stage == "capture"
    harness.assert_error_trace(result.error)
    harness.create.assert_not_awaited()
    assert result.screenshot_sha256 == ""
    assert "verification_screenshot_unavailable" in json.dumps(harness.records())
    harness.assert_private_data_absent(result, "private-device-error")


def test_total_timeout_includes_screenshot_capture(harness, monkeypatch):
    monkeypatch.setattr(subtasks, "VERIFICATION_TIMEOUT_SECONDS", 0.01)

    async def slow_capture():
        await asyncio.Event().wait()

    harness.device.get_screenshot.side_effect = slow_capture
    result = asyncio.run(harness.run())
    assert result.error == "verification_timeout"
    assert result.diagnostics.stage == "capture"
    harness.assert_error_trace(result.error)
    harness.create.assert_not_awaited()
    assert "verification_timeout" in json.dumps(harness.records())
    harness.assert_private_data_absent(result)


@pytest.mark.parametrize("stage", ["screenshot", "request"])
def test_cancel_is_propagated_without_retry(harness, stage):
    async def scenario():
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def pending():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async def pending_request(**kwargs):
            return await pending()

        if stage == "screenshot":
            harness.device.get_screenshot.side_effect = pending
        else:
            harness.create.side_effect = pending_request
        running = asyncio.create_task(harness.run())
        await asyncio.wait_for(started.wait(), timeout=1)
        running.cancel("private-cancel-message")
        with pytest.raises(asyncio.CancelledError):
            await running
        assert stopped.is_set()
        assert "private-cancel-message" not in harness.trace_file.read_text()
        assert harness.create.await_count == (1 if stage == "request" else 0)

    asyncio.run(scenario())


def test_unexpected_failure_remains_generic_and_safe(harness, monkeypatch):
    def broken_message(*args, **kwargs):
        raise RuntimeError("private-unexpected-error")

    monkeypatch.setattr(
        subtasks.MessageBuilder, "create_system_message", broken_message
    )
    result = asyncio.run(harness.run())
    assert result.error == "verification_unavailable"
    harness.assert_error_trace(result.error)
    harness.create.assert_not_awaited()
    harness.assert_private_data_absent(result, "private-unexpected-error")
