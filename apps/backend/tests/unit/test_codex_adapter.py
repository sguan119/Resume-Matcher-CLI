"""Unit tests for app.codex_adapter module."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.codex_adapter import (
    _build_json_prompt,
    _extract_from_code_fence,
    _get_semaphore,
    _try_parse,
    complete,
    complete_json,
)


# ---------------------------------------------------------------------------
# _extract_from_code_fence
# ---------------------------------------------------------------------------
class TestExtractFromCodeFence:
    def test_valid_json_fence(self) -> None:
        text = '```json\n{"key": "value"}\n```'
        assert _extract_from_code_fence(text) == '{"key": "value"}'

    def test_fence_with_surrounding_text(self) -> None:
        text = 'Here is the JSON:\n```json\n{"a": 1}\n```\nDone.'
        assert _extract_from_code_fence(text) == '{"a": 1}'

    def test_no_fences_returns_none(self) -> None:
        assert _extract_from_code_fence("just plain text") is None

    def test_empty_content_inside_fence(self) -> None:
        text = "```json\n\n```"
        result = _extract_from_code_fence(text)
        # Empty string after strip
        assert result == ""

    def test_non_json_fence_returns_none(self) -> None:
        text = "```python\nprint('hi')\n```"
        assert _extract_from_code_fence(text) is None

    def test_multiline_json_in_fence(self) -> None:
        text = '```json\n{\n  "a": 1,\n  "b": 2\n}\n```'
        result = _extract_from_code_fence(text)
        assert result is not None
        parsed = json.loads(result)
        assert parsed == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# _try_parse
# ---------------------------------------------------------------------------
class TestTryParse:
    def test_valid_json_dict_in_fence(self) -> None:
        raw = '```json\n{"name": "Alice", "skills": ["Python"]}\n```'
        result, error = _try_parse(raw)
        assert result == {"name": "Alice", "skills": ["Python"]}
        assert error == ""

    def test_valid_json_array_returns_error(self) -> None:
        raw = '```json\n[1, 2, 3]\n```'
        result, error = _try_parse(raw)
        assert result is None
        assert "Expected JSON object" in error

    def test_malformed_json_returns_error(self) -> None:
        raw = '```json\n{"key": broken}\n```'
        result, error = _try_parse(raw)
        assert result is None
        assert "JSON parse error" in error or "No JSON found" in error

    def test_truncated_json_with_empty_work_experience(self) -> None:
        data = {"workExperience": [], "skills": ["Python"]}
        raw = f"```json\n{json.dumps(data)}\n```"
        result, error = _try_parse(raw)
        assert result is None
        assert "truncated" in error.lower()

    def test_json_without_fence_uses_extract_json(self) -> None:
        raw = '{"name": "Bob", "skills": ["Go"]}'
        result, error = _try_parse(raw)
        assert result == {"name": "Bob", "skills": ["Go"]}
        assert error == ""

    def test_thinking_tags_with_json(self) -> None:
        raw = '<think>Let me think...</think>\n{"name": "Charlie", "skills": ["Rust"]}'
        result, error = _try_parse(raw)
        assert result == {"name": "Charlie", "skills": ["Rust"]}
        assert error == ""


# ---------------------------------------------------------------------------
# _build_json_prompt
# ---------------------------------------------------------------------------
class TestBuildJsonPrompt:
    def test_with_system_prompt(self) -> None:
        result = _build_json_prompt("You are a parser.", "Parse this resume")
        assert "[System]" in result
        assert "You are a parser." in result
        assert "[User]" in result
        assert "Parse this resume" in result
        assert "[Output Instructions]" in result
        assert "```json" in result

    def test_without_system_prompt(self) -> None:
        result = _build_json_prompt("", "Parse this resume")
        assert "[System]" not in result
        assert "[User]" in result
        assert "Parse this resume" in result
        assert "[Output Instructions]" in result

    def test_always_includes_json_fence_instruction(self) -> None:
        result = _build_json_prompt("sys", "user")
        assert "```json" in result
        assert "```" in result


# ---------------------------------------------------------------------------
# _get_semaphore
# ---------------------------------------------------------------------------
class TestGetSemaphore:
    def test_returns_semaphore(self) -> None:
        # Reset global state for test isolation
        import app.codex_adapter as mod
        mod._semaphore = None
        sem = _get_semaphore()
        assert isinstance(sem, asyncio.Semaphore)

    def test_returns_same_instance(self) -> None:
        import app.codex_adapter as mod
        mod._semaphore = None
        sem1 = _get_semaphore()
        sem2 = _get_semaphore()
        assert sem1 is sem2


# ---------------------------------------------------------------------------
# complete() — mock _run_codex
# ---------------------------------------------------------------------------
class TestComplete:
    @pytest.mark.asyncio
    async def test_normal_text_response(self) -> None:
        with patch("app.codex_adapter._run_codex", new_callable=AsyncMock) as mock:
            mock.return_value = "  This is a summary.  "
            result = await complete("Summarize this")
            assert result == "This is a summary."

    @pytest.mark.asyncio
    async def test_strips_markdown_fences(self) -> None:
        with patch("app.codex_adapter._run_codex", new_callable=AsyncMock) as mock:
            mock.return_value = "```text\nHello world\n```"
            result = await complete("Say hello")
            assert result == "Hello world"

    @pytest.mark.asyncio
    async def test_empty_response_raises(self) -> None:
        with patch("app.codex_adapter._run_codex", new_callable=AsyncMock) as mock:
            mock.return_value = "   "
            with pytest.raises(ValueError, match="Empty response"):
                await complete("Say hello")

    @pytest.mark.asyncio
    async def test_system_prompt_included(self) -> None:
        with patch("app.codex_adapter._run_codex", new_callable=AsyncMock) as mock:
            mock.return_value = "output"
            await complete("prompt", system_prompt="be helpful")
            call_args = mock.call_args[0][0]
            assert "[System]" in call_args
            assert "be helpful" in call_args

    @pytest.mark.asyncio
    async def test_no_system_prompt(self) -> None:
        with patch("app.codex_adapter._run_codex", new_callable=AsyncMock) as mock:
            mock.return_value = "output"
            await complete("prompt")
            call_args = mock.call_args[0][0]
            assert "[System]" not in call_args


# ---------------------------------------------------------------------------
# complete_json() — mock _run_codex
# ---------------------------------------------------------------------------
class TestCompleteJson:
    @pytest.mark.asyncio
    async def test_first_attempt_succeeds(self) -> None:
        response = '```json\n{"name": "Alice"}\n```'
        with patch("app.codex_adapter._run_codex", new_callable=AsyncMock) as mock:
            mock.return_value = response
            result = await complete_json("parse this")
            assert result == {"name": "Alice"}
            assert mock.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_succeeds_on_second_attempt(self) -> None:
        bad = "not json at all {{{"
        good = '```json\n{"name": "Bob"}\n```'
        with patch("app.codex_adapter._run_codex", new_callable=AsyncMock) as mock:
            mock.side_effect = [bad, good]
            result = await complete_json("parse this", retries=1)
            assert result == {"name": "Bob"}
            assert mock.call_count == 2

    @pytest.mark.asyncio
    async def test_all_retries_fail_repair_succeeds(self) -> None:
        bad = "broken json"
        repaired = '```json\n{"name": "Fixed"}\n```'
        with patch("app.codex_adapter._run_codex", new_callable=AsyncMock) as mock:
            # retries=1 → 2 generation attempts + 1 repair
            mock.side_effect = [bad, bad, repaired]
            result = await complete_json("parse this", retries=1)
            assert result == {"name": "Fixed"}
            assert mock.call_count == 3

    @pytest.mark.asyncio
    async def test_all_retries_and_repair_fail_raises(self) -> None:
        bad = "broken json"
        with patch("app.codex_adapter._run_codex", new_callable=AsyncMock) as mock:
            # retries=0 → 1 generation + 1 repair, all bad
            mock.side_effect = [bad, bad]
            with pytest.raises(ValueError, match="Failed to parse JSON"):
                await complete_json("parse this", retries=0)

    @pytest.mark.asyncio
    async def test_default_retries_is_two(self) -> None:
        bad = "not json"
        with patch("app.codex_adapter._run_codex", new_callable=AsyncMock) as mock:
            mock.return_value = bad
            with pytest.raises(ValueError):
                await complete_json("parse this")
            # 3 generation attempts (retries=2) + 1 repair = 4 calls
            assert mock.call_count == 4
