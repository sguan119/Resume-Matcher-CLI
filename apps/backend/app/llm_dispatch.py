"""Dispatch layer — routes complete/complete_json to the active backend.

Health/config functions always come from llm.py regardless of backend setting,
since they manage configuration and don't perform LLM inference.
"""

import shutil
from typing import Any

from app.llm import (
    LLMConfig,
    check_llm_health as _litellm_check_llm_health,
    get_llm_config,
    resolve_api_key,
)


async def check_llm_health(
    config: LLMConfig | None = None,
    *,
    include_details: bool = False,
    test_prompt: str | None = None,
) -> dict[str, Any]:
    """Check LLM health — returns healthy immediately when codex_cli is active."""
    from app.config import settings

    if getattr(settings, "llm_backend", "litellm") == "codex_cli":
        codex_bin = getattr(settings, "codex_bin", "codex")
        found = shutil.which(codex_bin) is not None
        if found:
            return {
                "healthy": True,
                "provider": "codex_cli",
                "model": "codex",
            }
        return {
            "healthy": False,
            "provider": "codex_cli",
            "model": "codex",
            "error_code": "codex_binary_not_found",
        }
    return await _litellm_check_llm_health(
        config, include_details=include_details, test_prompt=test_prompt
    )


async def complete(
    prompt: str,
    system_prompt: str | None = None,
    config: LLMConfig | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.7,
) -> str:
    """Dispatch to active backend."""
    impl = _get_complete()
    return await impl(prompt, system_prompt, config, max_tokens, temperature)


async def complete_json(
    prompt: str,
    system_prompt: str | None = None,
    config: LLMConfig | None = None,
    max_tokens: int = 4096,
    retries: int = 2,
) -> dict[str, Any]:
    """Dispatch to active backend."""
    impl = _get_complete_json()
    return await impl(prompt, system_prompt, config, max_tokens, retries)


def _get_complete():
    """Lazy import based on current settings."""
    from app.config import settings

    if getattr(settings, "llm_backend", "litellm") == "codex_cli":
        from app.codex_adapter import complete as fn
    else:
        from app.llm import complete as fn
    return fn


def _get_complete_json():
    """Lazy import based on current settings."""
    from app.config import settings

    if getattr(settings, "llm_backend", "litellm") == "codex_cli":
        from app.codex_adapter import complete_json as fn
    else:
        from app.llm import complete_json as fn
    return fn


__all__ = [
    "complete",
    "complete_json",
    "check_llm_health",
    "get_llm_config",
    "resolve_api_key",
    "LLMConfig",
]
