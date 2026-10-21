"""Bounded image heuristics for modal transitions, not CAPTCHA recognition.

Only the independently verified executor uses these checks. They compare the
observation with a fresh pre-action frame and ignore the status/navigation bars.
A stable dialog (including a stable CAPTCHA) is outside this detector's scope.
"""

import base64
from dataclasses import dataclass
from io import BytesIO
from statistics import fmean, pstdev

from PIL import Image

from AutoGLM_GUI.device_protocol import Screenshot


@dataclass(frozen=True)
class ScreenComparison:
    reason: str | None
    metrics: dict[str, float]


def _thumbnail(screenshot: Screenshot) -> list[int]:
    if screenshot.is_sensitive or screenshot.width <= 0 or screenshot.height <= 0:
        raise ValueError("sensitive or invalid screenshot")
    raw = base64.b64decode(screenshot.base64_data, validate=True)
    with Image.open(BytesIO(raw)) as image:
        if image.size != (screenshot.width, screenshot.height):
            raise ValueError("screenshot dimensions do not match image")
        return list(
            image.convert("L").resize((96, 160), Image.Resampling.BILINEAR).tobytes()
        )


def compare_modal_transition(
    observed: Screenshot, current: Screenshot
) -> ScreenComparison:
    """Reject obvious dimmed-overlay changes without exact pixel matching.

    Besides a newly dimmed background, detect a mostly blank central panel that
    fills while the dim outer background stays stable. This catches loading
    dialogs becoming interactive during inference, without interpreting content.
    Invalid frames raise ValueError so the caller can fail closed.
    """
    if (observed.width, observed.height) != (current.width, current.height):
        raise ValueError("screen dimensions changed")
    before, after = _thumbnail(observed), _thumbnail(current)
    outer = [
        y * 96 + x
        for y in range(16, 144)
        for x in range(96)
        if x < 4 or x >= 92 or y < 38 or y >= 128
    ]
    center = [y * 96 + x for y in range(56, 104) for x in range(19, 77)]
    outer_before = [before[i] for i in outer]
    outer_after = [after[i] for i in outer]
    metrics = {
        "outer_darkened_fraction": fmean(after[i] < before[i] - 24 for i in outer),
        "outer_brightened_fraction": fmean(after[i] > before[i] + 24 for i in outer),
        "outer_changed_fraction": fmean(abs(after[i] - before[i]) > 24 for i in outer),
        "outer_before_mean": fmean(outer_before),
        "outer_before_stddev": pstdev(outer_before),
        "outer_after_mean": fmean(outer_after),
        "center_changed_fraction": fmean(
            abs(after[i] - before[i]) > 24 for i in center
        ),
        "center_before_bright_fraction": fmean(before[i] >= 225 for i in center),
        "center_after_bright_fraction": fmean(after[i] >= 225 for i in center),
    }
    reason = None
    if (
        metrics["outer_darkened_fraction"] >= 0.70
        and metrics["outer_brightened_fraction"] <= 0.05
        and metrics["center_after_bright_fraction"] >= 0.20
    ):
        reason = "new_dimmed_modal"
    elif (
        metrics["outer_changed_fraction"] <= 0.08
        and metrics["outer_before_mean"] <= 155
        and metrics["outer_before_stddev"] <= 55
        and metrics["center_before_bright_fraction"] >= 0.80
        and metrics["center_changed_fraction"] >= 0.20
    ):
        reason = "loading_modal_changed"
    return ScreenComparison(reason, metrics)
