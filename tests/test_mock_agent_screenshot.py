"""Screenshot contracts for the mock device used by lifecycle/UI E2E tests."""

import base64
from io import BytesIO
from pathlib import Path

from PIL import Image

from tests.e2e.device_agent.mock_agent_server import MockAgentState


def test_default_mock_screenshot_contains_valid_pixels_with_matching_dimensions():
    state = MockAgentState()

    screenshot = state.get_screenshot_data()
    raw = base64.b64decode(screenshot["base64_data"], validate=True)
    with Image.open(BytesIO(raw)) as image:
        image.load()
        assert image.format == "PNG"
        assert image.size == (screenshot["width"], screenshot["height"])
        assert image.size == (1080, 2400)
    assert screenshot["is_sensitive"] is False
    assert state.get_screenshot_data() == screenshot
    screenshot["base64_data"] = ""
    assert state.get_screenshot_data()["base64_data"]


def test_loaded_scenario_supersedes_placeholder_and_tracks_state(
    sample_test_case: Path,
):
    state = MockAgentState()
    placeholder = state.get_screenshot_data()
    state.load_scenario(sample_test_case)
    machine = state.state_machine
    assert machine is not None

    home = state.get_screenshot_data()
    expected = machine.get_current_screenshot()
    assert home["base64_data"] == expected.base64_data
    assert (home["width"], home["height"]) == (expected.width, expected.height)
    assert home != placeholder

    machine.handle_tap(600, 2590)
    message = state.get_screenshot_data()
    assert message["base64_data"] == machine.get_current_screenshot().base64_data
    assert message != home
