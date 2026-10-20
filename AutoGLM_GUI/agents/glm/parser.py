import ast
from typing import Any


class GLMParser:
    @property
    def coordinate_scale(self) -> int:
        return 1000

    def parse(self, raw_response: str) -> dict[str, Any]:
        action_str = raw_response.strip()

        if action_str.startswith("finish("):
            return self._parse_finish(action_str)
        if action_str.startswith("do("):
            return self._parse_do(action_str)
        raise ValueError(f"Unknown action format: {action_str}")

    def _parse_finish(self, action_str: str) -> dict[str, Any]:
        try:
            params = self._extract_params(action_str, "finish")
            return {
                "_metadata": "finish",
                "message": params.get("message", "Task completed"),
            }
        except Exception as e:
            raise ValueError(f"Failed to parse finish action: {e}") from e

    def _parse_do(self, action_str: str) -> dict[str, Any]:
        try:
            params = self._extract_params(action_str, "do")
            action_name = params.get("action", "")

            result = {
                "_metadata": "do",
                "action": action_name,
            }

            for key, value in params.items():
                if key != "action":
                    result[key] = value

            return result
        except Exception as e:
            raise ValueError(f"Failed to parse do action: {e}") from e

    def _extract_params(self, action_str: str, function_name: str) -> dict[str, Any]:
        try:
            expression = ast.parse(action_str, mode="eval").body
        except SyntaxError as exc:
            raise ValueError("Malformed action call") from exc
        if (
            not isinstance(expression, ast.Call)
            or not isinstance(expression.func, ast.Name)
            or expression.func.id != function_name
            or expression.args
        ):
            raise ValueError(f"Expected {function_name}(keyword=value, ...) call")
        params: dict[str, Any] = {}
        for keyword in expression.keywords:
            if keyword.arg is None or keyword.arg in params:
                raise ValueError("Expanded or duplicate arguments are not supported")
            try:
                value = ast.literal_eval(keyword.value)
            except (ValueError, SyntaxError) as exc:
                # Existing GLM examples use bare identifiers such as message=ok.
                # Preserve these strings without accepting executable expressions.
                if isinstance(keyword.value, ast.Name):
                    value = keyword.value.id
                else:
                    raise ValueError("Action arguments must be literal values") from exc
            params[keyword.arg] = value
        if function_name == "finish" and (
            set(params) - {"message"}
            or not isinstance(params.get("message", "Task completed"), str)
        ):
            raise ValueError("finish accepts only a text message")
        return params
