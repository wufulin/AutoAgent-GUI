"""Execution accounting and independently labeled evaluation contracts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agents import Agent, RunContextWrapper
from agents.stream_events import RawResponsesStreamEvent

from AutoGLM_GUI.agents.gemini.async_agent import AsyncGeminiAgent
from AutoGLM_GUI.agents.glm.async_agent import AsyncGLMAgent
from AutoGLM_GUI.agents.qwen.async_agent import AsyncQwenAgent
from AutoGLM_GUI.config import ModelConfig
from AutoGLM_GUI.evaluation import evaluate_records, read_json_records
from AutoGLM_GUI.execution_metrics import (
    current_execution_metrics,
    execution_metrics,
    mark_model_calls_unavailable,
    record_model_call,
    record_usage,
)
from AutoGLM_GUI.layered_agent_service import LayeredTaskRun, PlannerMetricsHooks
from AutoGLM_GUI.subtasks import SubtaskRunContext

pytestmark = [pytest.mark.unit, pytest.mark.release_gate]


def _run(task_id="case", variant="v2", **overrides):
    return {
        "task_id": task_id,
        "variant": variant,
        "repetition": 1,
        "type": "execution_summary",
        "payload": {
            "route": "skill",
            "status": "completed",
            "stop_reason": "goal_verified",
            "elapsed_ms": 1000,
            "steps": 2,
            "subtasks": 1,
            "continuations": 0,
            "planner_calls": 0,
            "executor_calls": 2,
            "verifier_calls": 1,
            "input_tokens": 100,
            "output_tokens": 20,
            "usage_complete": True,
            "calls_complete": True,
            **overrides,
        },
    }


def _label(task_id="case", variant="v2", success=True, repetition=1):
    return {
        "task_id": task_id,
        "variant": variant,
        "repetition": repetition,
        "source": "human",
        "success": success,
    }


def test_metrics_count_requests_not_steps_and_preserve_partial_usage():
    with execution_metrics("skill") as metrics:
        successful = record_model_call("executor")
        record_usage(
            "executor",
            {"prompt_tokens": 20, "completion_tokens": 3},
            call_id=successful,
        )
        # Final cumulative chunks sometimes repeat; do not double-count them.
        record_usage(
            "executor",
            {"prompt_tokens": 20, "completion_tokens": 3},
            call_id=successful,
        )
        record_model_call("verifier")  # Provider raised before returning any usage.
        partial = metrics.summary("unknown", "provider_error", steps=12)
    assert partial["executor_calls"] == 1
    assert partial["verifier_calls"] == 1
    assert partial["steps"] == 12
    assert partial["input_tokens"] is None
    assert partial["output_tokens"] is None
    assert partial["usage_complete"] is False


def test_metrics_are_isolated_and_existing_context_is_restored():
    assert current_execution_metrics() is None
    assert record_model_call("executor") is None
    with execution_metrics("planner") as outer:
        record_model_call("planner")
        with execution_metrics("visual") as inner:
            record_model_call("executor")
            with execution_metrics("ignored", metrics=outer):
                record_usage("planner", input_tokens=12, output_tokens=5)
            assert current_execution_metrics() is inner
        assert current_execution_metrics() is outer
        assert inner.summary("failed", "error")["planner_calls"] == 0
        summary = outer.summary("completed", "goal_verified")
        assert summary["executor_calls"] == 0
        assert summary["input_tokens"] == 12
    assert current_execution_metrics() is None


def test_concurrent_calls_keep_usage_attached_to_the_correct_request():
    async def run():
        with execution_metrics("planner") as metrics:

            async def request(tokens):
                call_id = record_model_call("executor")
                await asyncio.sleep(0)
                record_usage(
                    "executor", input_tokens=tokens, output_tokens=1, call_id=call_id
                )

            await asyncio.gather(request(5), request(11))
        return metrics.summary("completed", "goal_verified")

    summary = asyncio.run(run())
    assert summary["executor_calls"] == 2
    assert summary["input_tokens"] == 16
    assert summary["output_tokens"] == 2
    assert summary["usage_complete"] is True


@pytest.mark.parametrize("missing_first_usage", [False, True])
def test_planner_usage_remains_bound_when_consumer_lags_two_requests(
    monkeypatch, missing_first_usage
):
    async def run():
        context = SubtaskRunContext(
            "planner-metrics-test", "session-metrics-test", None, 10, "config"
        )
        # This test only consumes SDK events; it must not initialize a device manager.
        monkeypatch.setattr(context, "close", AsyncMock())
        first_usage = (
            None if missing_first_usage else {"input_tokens": 11, "output_tokens": 3}
        )
        second_usage = {"input_tokens": 29, "output_tokens": 7}

        class DelayedConsumerResult:
            final_output = ""

            async def stream_events(self):
                for usage in (first_usage, second_usage):
                    yield RawResponsesStreamEvent(
                        data=SimpleNamespace(
                            type="response.completed",
                            response=SimpleNamespace(usage=usage),
                        )
                    )

        with execution_metrics("planner") as metrics:
            hooks = PlannerMetricsHooks()
            wrapper = RunContextWrapper(context=context)
            agent = Agent(name="metrics-test")
            # Both producer starts happen before either response is consumed.
            await hooks.on_llm_start(wrapper, agent, None, [])
            await hooks.on_llm_start(wrapper, agent, None, [])
            first_id, second_id = context.planner_call_ids
            assert first_id is not None and second_id is not None
            assert first_id != second_id
            task = LayeredTaskRun(
                task_id=context.task_id,
                session_id=context.session_id,
                result=DelayedConsumerResult(),
                context=context,
            )
            events = [event async for event in task.stream_events()]
            assert events[-1]["type"] == "done"
            assert not any(event["type"] == "error" for event in events)
            assert context.planner_call_ids == []
            before = metrics.summary("blocked", "no_verified_subtask")
            # Overwrite only the first receipt through the public API. The
            # second receipt must retain its own totals, even when the first
            # completion had no usage at all.
            record_usage("planner", input_tokens=0, output_tokens=0, call_id=first_id)
            after = metrics.summary("blocked", "no_verified_subtask")
            return before, after

    before, after = asyncio.run(run())
    assert before["planner_calls"] == after["planner_calls"] == 2
    assert before["input_tokens"] == (None if missing_first_usage else 40)
    assert before["output_tokens"] == (None if missing_first_usage else 10)
    assert before["usage_complete"] is (not missing_first_usage)
    assert after["input_tokens"] == 29
    assert after["output_tokens"] == 7
    assert after["usage_complete"] is True


def test_opaque_adapter_never_appears_to_have_complete_zero_usage():
    with execution_metrics("visual") as metrics:
        mark_model_calls_unavailable()
    summary = metrics.summary("failed", "external_executor")
    assert summary["calls_complete"] is False
    assert summary["usage_complete"] is False
    assert summary["input_tokens"] is None
    assert summary["output_tokens"] is None


def test_invalid_or_absent_usage_is_not_coerced_to_zero():
    with execution_metrics("visual") as metrics:
        call_id = record_model_call("executor")
        record_usage(
            "executor",
            {"prompt_tokens": True, "completion_tokens": -2},
            call_id=call_id,
        )
    summary = metrics.summary("completed", "goal_verified")
    assert summary["input_tokens"] is None
    assert summary["output_tokens"] is None


class _FakeStream:
    def __init__(self, usage):
        self.usage = usage
        self.closed = False

    async def __aiter__(self):
        yield SimpleNamespace(choices=[], usage=self.usage)

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("agent_class", [AsyncGLMAgent, AsyncQwenAgent])
def test_stream_provider_counts_usage_only_chunks_without_changing_protocol(
    agent_class,
):
    async def run():
        agent = object.__new__(agent_class)
        agent.model_config = ModelConfig(api_key="test-not-a-secret")
        agent._cancel_event = asyncio.Event()
        stream = _FakeStream(SimpleNamespace(prompt_tokens=23, completion_tokens=7))

        async def create(**kwargs):
            assert "stream_options" not in kwargs
            return stream

        agent.openai_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        with execution_metrics("visual") as metrics:
            chunks = [chunk async for chunk in agent._stream_openai([])]
        assert chunks == []
        assert stream.closed
        return metrics.summary("completed", "goal_verified")

    summary = asyncio.run(run())
    assert summary["executor_calls"] == 1
    assert summary["input_tokens"] == 23
    assert summary["output_tokens"] == 7


@pytest.mark.parametrize(
    "agent_class", [AsyncGLMAgent, AsyncQwenAgent, AsyncGeminiAgent]
)
def test_provider_failure_is_still_one_request_with_unknown_usage(agent_class):
    async def run():
        agent = object.__new__(agent_class)
        agent.model_config = ModelConfig(api_key="test-not-a-secret")
        agent._cancel_event = asyncio.Event()
        agent._context = []

        async def create(**kwargs):
            raise RuntimeError("provider unavailable")

        agent.openai_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        with execution_metrics("visual") as metrics:
            with pytest.raises(RuntimeError, match="provider unavailable"):
                if agent_class is AsyncGeminiAgent:
                    await agent._call_llm_with_tools()
                else:
                    _ = [chunk async for chunk in agent._stream_openai([])]
        return metrics.summary("failed", "provider_error")

    summary = asyncio.run(run())
    assert summary["executor_calls"] == 1
    assert summary["input_tokens"] is None
    assert summary["usage_complete"] is False


def test_general_vision_records_nonstreaming_provider_usage():
    async def run():
        agent = object.__new__(AsyncGeminiAgent)
        agent.model_config = ModelConfig(api_key="test-not-a-secret")
        agent._cancel_event = asyncio.Event()
        agent._context = []

        async def create(**kwargs):
            assert kwargs["tool_choice"] == "required"
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=31, completion_tokens=9),
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="页面已打开",
                            tool_calls=[
                                SimpleNamespace(
                                    function=SimpleNamespace(
                                        name="finish", arguments='{"message":"完成"}'
                                    )
                                )
                            ],
                        )
                    )
                ],
            )

        agent.openai_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        with execution_metrics("direct") as metrics:
            assert (await agent._call_llm_with_tools())[2] == "finish"
        return metrics.summary("completed", "goal_verified")

    summary = asyncio.run(run())
    assert summary["executor_calls"] == 1
    assert summary["input_tokens"] == 31
    assert summary["output_tokens"] == 9
    assert summary["usage_complete"] is True


def test_runtime_completed_without_label_is_not_real_success():
    report = evaluate_records([_run()])
    group = report["groups"]["skill"]
    assert group["runtime_completed"] == 1
    assert group["real_successes"] == 0
    assert group["real_success_rate"] is None
    assert group["false_success_rate"] is None
    assert group["unlabeled_completions"] == 1


def test_real_success_and_false_success_use_independent_labels():
    records = [
        _run("done"),
        _run("false"),
        _run("blocked", status="blocked"),
        _run("unlabeled", status="unknown"),
    ]
    labels = [
        _label("done"),
        _label("false", success=False),
        _label("blocked", success=False),
    ]
    group = evaluate_records(records, labels)["groups"]["skill"]
    assert group["runtime_completed"] == 2
    assert group["real_success_rate"] == pytest.approx(1 / 3, abs=1e-6)
    assert group["false_successes"] == 1
    assert group["false_success_rate"] == 0.5
    assert group["unlabeled_runs"] == 1


def test_partial_usage_and_opaque_calls_are_not_aggregated_as_complete():
    records = [
        _run("measured"),
        _run(
            "unknown",
            usage_complete=False,
            input_tokens=None,
            output_tokens=None,
            calls_complete=False,
        ),
    ]
    group = evaluate_records(records)["groups"]["skill"]
    assert group["model_calls"] is None
    assert group["executor_calls"] is None
    assert group["input_tokens"] is None
    assert group["usage_complete"] is False


def test_comparison_requires_matching_repetitions_and_human_labels():
    base, candidate = _run(variant="v1"), _run()
    candidate["repetition"] = 2
    comparison = evaluate_records([base, candidate], baseline="v1", candidate="v2")[
        "comparison"
    ]
    assert comparison["matched_pairs"] == 0
    assert comparison["savings"] is None
    candidate["repetition"] = 1
    comparison = evaluate_records([base, candidate], baseline="v1", candidate="v2")[
        "comparison"
    ]
    assert comparison["reason"] == "independent_labels_missing"
    assert comparison["savings"] is None


def test_comparison_uses_only_matched_cases_and_rejects_quality_regression():
    records = [
        _run(variant="v1", elapsed_ms=2000, executor_calls=4),
        _run(elapsed_ms=1000),
        _run("unmatched", elapsed_ms=20000),
    ]
    labels = [_label(variant="v1"), _label()]
    comparison = evaluate_records(records, labels, baseline="v1", candidate="v2")[
        "comparison"
    ]
    assert comparison["matched_pairs"] == 1
    assert comparison["candidate_unmatched"] == 1
    assert comparison["savings"]["elapsed_ms_reduction_percent"] == 50
    assert comparison["savings"]["model_calls_reduction_percent"] == 40
    labels[1]["success"] = False
    comparison = evaluate_records(records, labels, baseline="v1", candidate="v2")[
        "comparison"
    ]
    assert comparison["savings"] is None
    assert comparison["reason"] == "outcome_quality_regressed"


def test_synthetic_fixture_cannot_be_reported_as_measured_savings():
    records = [_run(variant="v1"), _run()]
    records[1]["synthetic"] = True
    report = evaluate_records(
        records, [_label(variant="v1"), _label()], baseline="v1", candidate="v2"
    )
    assert report["comparison"]["savings"] is None
    assert report["comparison"]["reason"] == "synthetic_fixture_no_empirical_claim"


@pytest.mark.parametrize(
    "change",
    [
        {"elapsed_ms": float("nan")},
        {"executor_calls": -1},
        {"usage_complete": True, "input_tokens": None},
        {"status": "success"},
    ],
)
def test_invalid_metrics_fail_instead_of_silently_improving_results(change):
    with pytest.raises(ValueError):
        evaluate_records([_run(**change)])


def test_duplicate_runs_labels_and_automated_labels_are_rejected():
    with pytest.raises(ValueError, match="Duplicate run"):
        evaluate_records([_run(), _run()])
    with pytest.raises(ValueError, match="Duplicate human label"):
        evaluate_records([_run()], [_label(), _label()])
    label = {**_label(), "source": "model"}
    with pytest.raises(ValueError, match="source='human'"):
        evaluate_records([_run()], [label])


def test_read_exported_events_and_synthetic_benchmark(tmp_path):
    path = tmp_path / "events.json"
    event = _run()
    event["payload"] = {"summary": event["payload"]}
    path.write_text(json.dumps({"events": [{"type": "step"}, event]}))
    report = evaluate_records(read_json_records(path))
    assert report["groups"]["skill"]["runs"] == 1
    fixture = Path(__file__).parent / "fixtures" / "execution_benchmark.jsonl"
    report = evaluate_records(read_json_records(fixture), group_by="variant")
    assert report["groups"]["v2-fixture"]["status_counts"] == {
        "blocked": 1,
        "completed": 2,
        "needs_user": 1,
        "unknown": 1,
    }
    assert report["groups"]["v2-fixture"]["unlabeled_runs"] == 5
