"""Codex CLI adapter — routes LLM calls through codex exec subprocess.

Exposes the same complete() and complete_json() signatures as llm.py,
allowing transparent switching via the dispatch layer (llm_dispatch.py).
"""

import asyncio
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from app.llm import LLMConfig, _extract_json

logger = logging.getLogger(__name__)

CODEX_BIN = os.getenv("CODEX_BIN", "codex")
CODEX_WORKDIR = os.getenv("CODEX_WORKDIR", "/tmp/codex-runner")

# Lazy semaphore — avoids event-loop binding issues at import time
# (known footgun with Uvicorn lifespan and pytest-asyncio)
_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    """Get or create the global semaphore lazily.

    Safe in CPython asyncio: no `await` between the None check and assignment,
    so no concurrent coroutine can interleave inside this synchronous function.
    The GIL guarantees the check-then-set is atomic in practice.
    """
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(1)
    return _semaphore


async def _run_codex(prompt: str, schema_path: str | None = None) -> str:
    """Run codex exec as subprocess, return last message text.

    Args:
        prompt: The prompt text to send to Codex.
        schema_path: Optional path to a JSON Schema file. When provided,
                     passes --output-schema to constrain model output shape.

    Raises:
        ValueError: If the CLI fails or returns empty output.
    """
    out_path = Path(CODEX_WORKDIR) / f"{uuid.uuid4()}.txt"
    prompt_path = Path(CODEX_WORKDIR) / f"{uuid.uuid4()}.prompt.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Write prompt to temp file, then pass as stdin file handle.
    # - No shell involvement (no shell=True, no injection surface)
    # - No argv length limits (resumes can be very long)
    # - Verified: `codex exec` reads from stdin when `-` is passed as prompt arg
    prompt_path.write_text(prompt, encoding="utf-8")

    try:
        async with _get_semaphore():
            # All flags verified against `codex exec --help`
            cmd = [
                CODEX_BIN, "exec",
                "--skip-git-repo-check",        # run outside git repo
                "--sandbox", "read-only",        # prevent file writes
                "--ephemeral",                   # no session persistence
                "-o", str(out_path),             # capture last message to file
                "-",                             # read prompt from stdin
            ]

            if schema_path:
                cmd.extend(["--output-schema", schema_path])

            # Input: open temp file as stdin file handle — no shell, no pipe,
            # no quoting issues. Works on all platforms.
            with open(prompt_path, "rb") as prompt_file:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=prompt_file,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=CODEX_WORKDIR,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=300,
                )

        if proc.returncode != 0:
            error_msg = stderr.decode("utf-8", errors="replace")
            logger.error("codex exec failed (rc=%d): %s", proc.returncode, error_msg[:500])
            raise ValueError(f"Codex CLI failed (rc={proc.returncode}): {error_msg[:200]}")

        # Output capture — try -o file first, fall back to stdout
        if out_path.exists():
            result = out_path.read_text(encoding="utf-8")
            out_path.unlink(missing_ok=True)
            return result

        # Fallback: parse stdout. WARNING: stdout may contain progress/status
        # messages mixed with actual output. This fallback needs testing.
        raw_stdout = stdout.decode("utf-8", errors="replace")
        if not raw_stdout.strip():
            raise ValueError("Codex CLI returned empty output")
        return raw_stdout

    finally:
        # Clean up temp files
        prompt_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)


async def complete(
    prompt: str,
    system_prompt: str | None = None,
    config: LLMConfig | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.7,
) -> str:
    """Make a plain text completion request.

    Raises ValueError on failure (matches llm.py behavior).
    """
    parts = []
    if system_prompt:
        parts.append(f"[System]\n{system_prompt}")
    parts.append(f"[User]\n{prompt}")
    parts.append(
        "[Output Instructions]\n"
        "Respond with ONLY the requested text. No markdown, no explanation."
    )
    full_prompt = "\n\n".join(parts)

    raw = await _run_codex(full_prompt)

    # Strip markdown artifacts if present
    raw = raw.strip()
    raw = re.sub(r"^```\w*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    result = raw.strip()

    if not result:
        raise ValueError("Empty response from Codex CLI")
    return result


def _extract_from_code_fence(text: str) -> str | None:
    """Extract content from ```json ... ``` blocks."""
    match = re.search(r"```json\s*\n(.*?)\n\s*```", text, re.DOTALL)
    return match.group(1).strip() if match else None


def _try_parse(raw: str) -> tuple[dict[str, Any] | None, str]:
    """Attempt extraction then parse. Returns (result, error_msg).

    Note: Pydantic validation is NOT done here — callers handle that.
    This only guarantees syntactically valid JSON dict output.
    """
    # Layer 1: Code fence extraction (most reliable if model follows instructions)
    json_str = _extract_from_code_fence(raw)

    # Layer 2: Reuse llm.py's _extract_json (brace-counting + thinking tag strip)
    if json_str is None:
        try:
            json_str = _extract_json(raw)
        except ValueError:
            return None, "No JSON found in output"

    # Layer 3: Parse
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}"

    if not isinstance(data, dict):
        return None, f"Expected JSON object, got {type(data).__name__}"

    # Skip truncation check for codex_cli — the heuristic is too aggressive
    # for this model and causes spurious retries. Pydantic validation in the
    # caller will catch genuinely malformed data.

    return data, ""


_REPAIR_PROMPT = """The following JSON output is malformed or incomplete.

--- Raw Output ---
{raw_output}

--- Error ---
{error}

Fix the JSON and return ONLY the corrected JSON wrapped in ```json fences. No explanation."""


async def _repair_json(raw_output: str, error: str) -> str:
    """Send malformed output back to CLI for repair.

    Note: This calls _run_codex(), which acquires the semaphore.
    Since this is called AFTER the main generation's semaphore is released,
    there is no deadlock — but it does add another full CLI invocation latency.
    """
    prompt = _REPAIR_PROMPT.format(
        raw_output=raw_output[:4000],  # truncate to avoid token explosion
        error=error[:1000],
    )
    return await _run_codex(prompt)


def _build_json_prompt(system_prompt: str, user_prompt: str) -> str:
    """Build prompt that instructs CLI to output JSON.

    Note: The existing prompts in app/prompts/ already contain schema examples
    and JSON instructions. We add a reinforcing suffix, not a replacement.
    """
    parts = []
    if system_prompt:
        parts.append(f"[System]\n{system_prompt}")
    parts.append(f"[User]\n{user_prompt}")
    parts.append(
        "[Output Instructions]\n"
        "You MUST output ONLY valid JSON wrapped in ```json and ``` markdown fences.\n"
        "No explanation, no markdown outside the fence, no extra text."
    )
    return "\n\n".join(parts)


async def complete_json(
    prompt: str,
    system_prompt: str | None = None,
    config: LLMConfig | None = None,
    max_tokens: int = 4096,
    retries: int = 2,
) -> dict[str, Any]:
    """Make a completion request expecting JSON response.

    Raises ValueError on failure (matches llm.py behavior).
    """
    full_prompt = _build_json_prompt(system_prompt or "", prompt)

    # Schema path placeholder — add schema file mapping as enhancement (Phase 5).
    schema_path: str | None = None

    last_error = ""
    raw = ""
    for attempt in range(retries + 1):
        raw = await _run_codex(full_prompt, schema_path=schema_path)
        result, error = _try_parse(raw)
        if result is not None:
            return result
        last_error = error
        logger.warning("JSON parse failed (attempt %d/%d): %s", attempt + 1, retries + 1, error)

    # All generation attempts failed — try one repair pass on the last raw output
    logger.info("Attempting repair pass after %d failed generations", retries + 1)
    try:
        repaired_raw = await _repair_json(raw, last_error)
        result, repair_error = _try_parse(repaired_raw)
        if result is not None:
            logger.info("Repair pass succeeded")
            return result
        last_error = f"Repair also failed: {repair_error}"
    except ValueError as e:
        last_error = f"Repair CLI call failed: {e}"

    raise ValueError(f"Failed to parse JSON after {retries + 1} attempts + 1 repair: {last_error}")
