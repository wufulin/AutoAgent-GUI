"""Offline execution evaluation using independent human outcome labels.

Runtime ``completed`` means the runtime accepted its evidence; it is never used
as a human label. Comparisons pair task/case IDs and repetition numbers, and do
not report savings when labels are absent or outcome quality regresses.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_CALL_FIELDS = ("planner_calls", "executor_calls", "verifier_calls")
_COUNTER_FIELDS = (*_CALL_FIELDS, "steps", "subtasks", "continuations")
_TOKEN_FIELDS = ("input_tokens", "output_tokens")
_STATUSES = {
    "completed",
    "partial",
    "blocked",
    "failed",
    "cancelled",
    "needs_user",
    "unknown",
}


def read_json_records(path: Path) -> list[dict[str, Any]]:
    """Read JSONL, a JSON array, or an exported task ``{events: [...]}``."""
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return []
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = [json.loads(line) for line in content.splitlines() if line.strip()]
    if isinstance(parsed, dict):
        parsed = parsed.get("events", [parsed])
    if not isinstance(parsed, list) or any(not isinstance(row, dict) for row in parsed):
        raise ValueError(f"{path}: expected JSON objects or an events array")
    return parsed


def _identity(record: dict[str, Any]) -> tuple[str, str, int]:
    variant = record.get("variant")
    task_id = record.get("case_id", record.get("task_id"))
    repetition = record.get("repetition", 1)
    if not isinstance(variant, str) or not variant.strip():
        raise ValueError(
            "Every run/label needs a nonempty variant (defaults to run route)"
        )
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError(
            "Every run/label needs a nonempty task_id or benchmark case_id"
        )
    if type(repetition) is not int or repetition < 1:
        raise ValueError("repetition must be a positive integer")
    return variant, task_id, repetition


def _number(value: Any, name: str, *, integer: bool = False) -> int | float:
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or value < 0
        or (integer and type(value) is not int)
    ):
        raise ValueError(
            f"{name} must be a finite nonnegative {'integer' if integer else 'number'}"
        )
    return value


def _runs(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for record in records:
        event_type = record.get("event_type", record.get("type"))
        if event_type is not None and event_type != "execution_summary":
            continue
        payload = record.get("payload", record.get("data", record))
        if not isinstance(payload, dict):
            raise ValueError("execution_summary payload must be an object")
        summary = payload.get("summary", payload)
        if not isinstance(summary, dict):
            raise ValueError("execution_summary summary must be an object")
        route = summary.get("route")
        if not isinstance(route, str) or not route.strip():
            raise ValueError("execution_summary needs a nonempty route")
        run = {**record, **payload, **summary}
        run.setdefault("variant", route)
        identity = _identity(run)
        if identity in seen:
            raise ValueError(f"Duplicate run for variant/task/repetition: {identity}")
        seen.add(identity)
        if run.get("status") not in _STATUSES:
            raise ValueError(f"Unsupported execution status: {run.get('status')!r}")
        _number(run.get("elapsed_ms"), "elapsed_ms")
        for name in _COUNTER_FIELDS:
            _number(run.get(name), name, integer=True)
        for name in _TOKEN_FIELDS:
            if run.get(name) is not None:
                _number(run[name], name, integer=True)
        if type(run.get("usage_complete")) is not bool:
            raise ValueError("usage_complete must be a boolean")
        run.setdefault("calls_complete", True)
        if type(run["calls_complete"]) is not bool:
            raise ValueError("calls_complete must be a boolean")
        if run["usage_complete"] and (
            not run["calls_complete"]
            or any(run.get(name) is None for name in _TOKEN_FIELDS)
        ):
            raise ValueError(
                "Complete usage requires complete calls and both token counts"
            )
        run["_key"] = identity
        runs.append(run)
    return runs


def _labels(records: Iterable[dict[str, Any]]) -> dict[tuple[str, str, int], bool]:
    labels: dict[tuple[str, str, int], bool] = {}
    for record in records:
        key = _identity(record)
        if key in labels:
            raise ValueError(f"Duplicate human label: {key}")
        if record.get("source") != "human" or type(record.get("success")) is not bool:
            raise ValueError("Labels require source='human' and boolean success")
        labels[key] = record["success"]
    return labels


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = math.floor(index)
    upper = math.ceil(index)
    return round(
        ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower), 3
    )


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _aggregate(
    runs: list[dict[str, Any]], labels: dict[tuple[str, str, int], bool]
) -> dict[str, Any]:
    labeled = [run for run in runs if run["_key"] in labels]
    completed = [run for run in runs if run["status"] == "completed"]
    labeled_completed = [run for run in completed if run["_key"] in labels]
    real_successes = sum(labels[run["_key"]] for run in labeled)
    false_successes = sum(not labels[run["_key"]] for run in labeled_completed)
    calls_complete = all(run["calls_complete"] for run in runs)
    usage_complete = all(run["usage_complete"] for run in runs)
    return {
        "runs": len(runs),
        "synthetic_runs": sum(bool(run.get("synthetic")) for run in runs),
        "status_counts": dict(sorted(Counter(run["status"] for run in runs).items())),
        "runtime_completed": len(completed),
        "labeled_runs": len(labeled),
        "unlabeled_runs": len(runs) - len(labeled),
        "label_coverage": _rate(len(labeled), len(runs)),
        "real_successes": real_successes,
        "real_success_rate": _rate(real_successes, len(labeled)),
        "false_successes": false_successes,
        "false_success_rate": _rate(false_successes, len(labeled_completed)),
        "unlabeled_completions": len(completed) - len(labeled_completed),
        "latency_ms": {
            "p50": _percentile([run["elapsed_ms"] for run in runs], 0.5),
            "p95": _percentile([run["elapsed_ms"] for run in runs], 0.95),
            "total": round(sum(run["elapsed_ms"] for run in runs), 3),
        },
        **{
            name: sum(run[name] for run in runs)
            if name not in _CALL_FIELDS or calls_complete
            else None
            for name in _COUNTER_FIELDS
        },
        "model_calls": sum(run[name] for run in runs for name in _CALL_FIELDS)
        if calls_complete
        else None,
        "calls_complete": calls_complete,
        "usage_complete": usage_complete,
        **{
            name: sum(run[name] for run in runs)
            if usage_complete and all(run.get(name) is not None for run in runs)
            else None
            for name in _TOKEN_FIELDS
        },
    }


def _comparison(
    runs: list[dict[str, Any]],
    labels: dict[tuple[str, str, int], bool],
    baseline: str,
    candidate: str,
) -> dict[str, Any]:
    if baseline == candidate:
        raise ValueError("baseline and candidate variants must differ")
    by_variant = {
        variant: {run["_key"][1:]: run for run in runs if run["variant"] == variant}
        for variant in (baseline, candidate)
    }
    left, right = by_variant[baseline], by_variant[candidate]
    paired = sorted(left.keys() & right.keys())
    result: dict[str, Any] = {
        "baseline": baseline,
        "candidate": candidate,
        "matched_pairs": len(paired),
        "baseline_unmatched": len(left.keys() - right.keys()),
        "candidate_unmatched": len(right.keys() - left.keys()),
        "savings": None,
    }
    if not paired:
        result["reason"] = "no_matched_task_repetitions"
        return result
    baseline_stats = _aggregate([left[key] for key in paired], labels)
    candidate_stats = _aggregate([right[key] for key in paired], labels)
    result.update(baseline_matched=baseline_stats, candidate_matched=candidate_stats)
    if any(stats["synthetic_runs"] for stats in (baseline_stats, candidate_stats)):
        result["reason"] = "synthetic_fixture_no_empirical_claim"
        return result
    if any(stats["unlabeled_runs"] for stats in (baseline_stats, candidate_stats)):
        result["reason"] = "independent_labels_missing"
        return result
    if (
        candidate_stats["real_successes"] < baseline_stats["real_successes"]
        or candidate_stats["false_successes"] > baseline_stats["false_successes"]
    ):
        result["reason"] = "outcome_quality_regressed"
        return result
    savings: dict[str, float | None] = {}
    for name in ("elapsed_ms", "model_calls", *_TOKEN_FIELDS):
        before = (
            baseline_stats["latency_ms"]["total"]
            if name == "elapsed_ms"
            else baseline_stats[name]
        )
        after = (
            candidate_stats["latency_ms"]["total"]
            if name == "elapsed_ms"
            else candidate_stats[name]
        )
        savings[f"{name}_reduction_percent"] = (
            round((before - after) / before * 100, 3)
            if before is not None and after is not None and before > 0
            else None
        )
    result.update(savings=savings, reason="matched_labeled_observations_only")
    return result


def evaluate_records(
    records: Iterable[dict[str, Any]],
    labels: Iterable[dict[str, Any]] = (),
    *,
    group_by: str = "route",
    baseline: str | None = None,
    candidate: str | None = None,
) -> dict[str, Any]:
    """Aggregate exported executions without treating runtime claims as truth."""
    if group_by not in {"route", "variant"}:
        raise ValueError("group_by must be route or variant")
    if (baseline is None) != (candidate is None):
        raise ValueError("Specify both baseline and candidate")
    runs, label_map = _runs(records), _labels(labels)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        grouped[run[group_by]].append(run)
    report: dict[str, Any] = {
        "schema_version": 1,
        "group_by": group_by,
        "groups": {
            key: _aggregate(value, label_map) for key, value in sorted(grouped.items())
        },
        "unused_labels": len(label_map.keys() - {run["_key"] for run in runs}),
        "definitions": {
            "real_success_rate": "human successes / human-labeled runs",
            "false_success_rate": "human failures reported completed / human-labeled completed runs",
            "percentiles": "linear interpolation of observed samples; no statistical significance claim",
            "model_calls": "observed logical provider requests, including errors; SDK HTTP retries excluded",
            "comparison": "same task_id (or case_id) and repetition; matched subset only",
        },
    }
    if baseline is not None and candidate is not None:
        report["comparison"] = _comparison(runs, label_map, baseline, candidate)
    return report
