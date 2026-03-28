"""Unit tests for app.llm_dispatch module."""

from unittest.mock import AsyncMock, patch

import pytest

import app.llm as llm_module
import app.llm_dispatch as dispatch


# ---------------------------------------------------------------------------
# Re-export tests — health/config always come from llm.py
# ---------------------------------------------------------------------------
class TestReExports:
    def test_check_llm_health_is_same(self) -> None:
        assert dispatch.check_llm_health is llm_module.check_llm_health

    def test_get_llm_config_is_same(self) -> None:
        assert dispatch.get_llm_config is llm_module.get_llm_config

    def test_resolve_api_key_is_same(self) -> None:
        assert dispatch.resolve_api_key is llm_module.resolve_api_key

    def test_llm_config_is_same(self) -> None:
        assert dispatch.LLMConfig is llm_module.LLMConfig


# ---------------------------------------------------------------------------
# Dispatch routing — default (litellm)
# ---------------------------------------------------------------------------
class TestDispatchLitellm:
    @pytest.mark.asyncio
    async def test_complete_routes_to_llm(self) -> None:
        with patch("app.llm_dispatch._get_complete") as mock_get:
            inner = AsyncMock(return_value="litellm response")
            mock_get.return_value = inner
            result = await dispatch.complete("hello")
            assert result == "litellm response"
            inner.assert_called_once()

    @pytest.mark.asyncio
    async def test_complete_json_routes_to_llm(self) -> None:
        with patch("app.llm_dispatch._get_complete_json") as mock_get:
            inner = AsyncMock(return_value={"key": "val"})
            mock_get.return_value = inner
            result = await dispatch.complete_json("hello")
            assert result == {"key": "val"}
            inner.assert_called_once()


# ---------------------------------------------------------------------------
# Dispatch routing — _get_complete / _get_complete_json internals
# ---------------------------------------------------------------------------
class TestGetCompleteRouting:
    def test_default_routes_to_llm(self) -> None:
        with patch("app.config.settings") as mock_settings:
            mock_settings.llm_backend = "litellm"
            fn = dispatch._get_complete()
            assert fn is llm_module.complete

    def test_codex_cli_routes_to_codex_adapter(self) -> None:
        with patch("app.config.settings") as mock_settings:
            mock_settings.llm_backend = "codex_cli"
            fn = dispatch._get_complete()
            from app.codex_adapter import complete as codex_complete
            assert fn is codex_complete

    def test_missing_llm_backend_defaults_to_litellm(self) -> None:
        """When llm_backend attr doesn't exist, getattr default kicks in."""
        with patch("app.config.settings", spec=[]) as mock_settings:
            # spec=[] means no attributes → getattr returns default "litellm"
            fn = dispatch._get_complete()
            assert fn is llm_module.complete


class TestGetCompleteJsonRouting:
    def test_default_routes_to_llm(self) -> None:
        with patch("app.config.settings") as mock_settings:
            mock_settings.llm_backend = "litellm"
            fn = dispatch._get_complete_json()
            assert fn is llm_module.complete_json

    def test_codex_cli_routes_to_codex_adapter(self) -> None:
        with patch("app.config.settings") as mock_settings:
            mock_settings.llm_backend = "codex_cli"
            fn = dispatch._get_complete_json()
            from app.codex_adapter import complete_json as codex_complete_json
            assert fn is codex_complete_json


# ---------------------------------------------------------------------------
# Signature tests — ensure dispatch accepts expected kwargs
# ---------------------------------------------------------------------------
class TestSignatures:
    @pytest.mark.asyncio
    async def test_complete_accepts_all_kwargs(self) -> None:
        with patch("app.llm_dispatch._get_complete") as mock_get:
            inner = AsyncMock(return_value="ok")
            mock_get.return_value = inner
            result = await dispatch.complete(
                prompt="hello",
                system_prompt="be helpful",
                config=None,
                max_tokens=2048,
                temperature=0.5,
            )
            assert result == "ok"
            inner.assert_called_once_with("hello", "be helpful", None, 2048, 0.5)

    @pytest.mark.asyncio
    async def test_complete_json_accepts_all_kwargs(self) -> None:
        with patch("app.llm_dispatch._get_complete_json") as mock_get:
            inner = AsyncMock(return_value={"k": "v"})
            mock_get.return_value = inner
            result = await dispatch.complete_json(
                prompt="hello",
                system_prompt="be helpful",
                config=None,
                max_tokens=2048,
                retries=3,
            )
            assert result == {"k": "v"}
            inner.assert_called_once_with("hello", "be helpful", None, 2048, 3)
