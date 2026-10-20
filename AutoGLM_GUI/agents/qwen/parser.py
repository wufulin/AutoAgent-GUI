import ast
import re
from typing import Any


class QwenParser:
    """Parse Qwen model output into structured actions.

    Supports three action formats:
    - do(action="Tap", element=[x, y])
    - finish(message="done")
    - info(question="what to send?")
    """

    @property
    def coordinate_scale(self) -> int:
        return 1000

    def parse(self, action_str: str) -> dict[str, Any]:
        """Parse a raw action string into a structured dictionary.

        Args:
            action_str: Raw action string (e.g., "do(action=\"Tap\", element=[100, 200])")

        Returns:
            dict with '_metadata' key indicating action type.

        Raises:
            ValueError: If parsing fails.
        """
        response = action_str.strip()
        if response.startswith("<answer>") and response.endswith("</answer>"):
            response = response[len("<answer>") : -len("</answer>")].strip()
        try:
            return self._parse_call(response)
        except (SyntaxError, ValueError):
            # Coordinate-bracket repair is bounded to do() actions. Never repair
            # a malformed completion request into a successful finish.
            if response.startswith("do("):
                repaired = self._re_parse(response)
                if repaired != response:
                    return self._parse_call(repaired)
            raise ValueError("Malformed model action") from None

    @staticmethod
    def _parse_call(response: str) -> dict[str, Any]:
        call = ast.parse(response, mode="eval").body
        if (
            not isinstance(call, ast.Call)
            or not isinstance(call.func, ast.Name)
            or call.func.id not in {"do", "info", "finish"}
            or call.args
        ):
            raise ValueError("Expected do, info or finish with keyword arguments")
        result: dict[str, Any] = {"_metadata": call.func.id}
        for keyword in call.keywords:
            if keyword.arg is None or keyword.arg in result:
                raise ValueError("Expanded or duplicate arguments are not supported")
            result[keyword.arg] = ast.literal_eval(keyword.value)
        if call.func.id == "finish" and (
            set(result) - {"_metadata", "message"}
            or not isinstance(result.get("message", ""), str)
        ):
            raise ValueError("finish accepts only a text message")
        return result

    @staticmethod
    def _re_parse(response: str) -> str:
        """Fix common bracket formatting errors with regex."""
        answer_matches = re.findall(
            r"<answer>(.*?)(?:</answer>|$)", response, re.DOTALL
        )
        if answer_matches:
            response = answer_matches[-1].strip()

        response = re.sub(
            r"\[(\s*-?\d+(?:\s*,\s*-?\d+)*\s*)\)(?=\s*\))", r"[\1])", response
        )
        response = re.sub(
            r"\[(\s*-?\d+(?:\s*,\s*-?\d+)*\s*)\)(?=\s*,)", r"[\1]", response
        )
        response = re.sub(r"\[(\s*-?\d+(?:\s*,\s*-?\d+)*\s*)\)\s*$", r"[\1])", response)
        return response

    @staticmethod
    def parse_response(content: str) -> tuple[str, str]:
        """Parse raw model output into (thinking, action) parts.

        Three-tier strategy:
        1. <answer> tags (primary anchor)
        2. Keyword fallback: finish(message= / do(action=
        3. Return empty thinking + full content as action

        Args:
            content: Full raw model response text.

        Returns:
            (thinking_text, action_text) tuple.
        """

        if "<answer>" in content:
            answer_start = content.find("<answer>")
            answer_end = content.find("</answer>", answer_start)
            if answer_end == -1:
                answer_end = len(content)
            action = content[answer_start + len("<answer>") : answer_end].strip()
            thinking_part = content[:answer_start]
            thinking_part = (
                thinking_part.replace("<think>", "")
                .replace("</think>", "")
                .replace("<thought>", "")
                .replace("</thought>", "")
                .strip()
            )
            return thinking_part, action

        if "finish(message=" in content:
            parts = content.split("finish(message=", 1)
            thinking = (
                parts[0]
                .replace("<think>", "")
                .replace("</think>", "")
                .replace("<thought>", "")
                .replace("</thought>", "")
                .strip()
            )
            action = "finish(message=" + parts[1].strip()
            return thinking, action

        if "do(action=" in content:
            parts = content.split("do(action=", 1)
            thinking = (
                parts[0]
                .replace("<think>", "")
                .replace("</think>", "")
                .replace("<thought>", "")
                .replace("</thought>", "")
                .strip()
            )
            action = "do(action=" + parts[1].strip()
            return thinking, action

        return "", content
