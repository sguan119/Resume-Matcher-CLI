# Codex CLI Migration Plan

> Replace all LiteLLM HTTP API calls with OpenAI Codex CLI subprocess calls, using ChatGPT subscription auth.

## Background

The backend currently routes all LLM calls through LiteLLM (`apps/backend/app/llm.py`). There are 12 call sites across 7 files, all funneled through two functions: `complete()` (plain text) and `complete_json()` (JSON output). The goal is to replace this layer with Codex CLI invocations running under a ChatGPT subscription, eliminating per-token API costs.

### Current Architecture

```
FastAPI Router
  → Service Layer (parser, improver, refiner, cover_letter, enrichment)
    → llm.py (complete / complete_json)
      → LiteLLM Router
        → OpenAI / Anthropic / etc. HTTP API
```

### Target Architecture

```
FastAPI Router
  → Service Layer (unchanged)
    → codex_adapter.py (complete / complete_json — same signatures)
      → codex exec subprocess
        → ChatGPT subscription (auth.json mounted)
```

**Key constraint:** Only `llm.py` changes. Upper-layer services retain their existing call order, which already encodes the correct dependency chain between scenarios.

---

## Prerequisites — CLI Flags Verified

All flags verified against `codex exec --help` output:

- [x] `codex exec` subcommand — accepts `[PROMPT]` as positional arg, or reads from stdin when `-` is used
- [x] `-o, --output-last-message <FILE>` — writes last agent message to file
- [x] `-s, --sandbox read-only` — sandbox policy with values: `read-only`, `workspace-write`, `danger-full-access`
- [x] `--ephemeral` — run without persisting session files
- [x] `--skip-git-repo-check` — allow running outside a Git repo
- [x] `--output-schema <FILE>` — JSON Schema file to constrain model's final response shape
- [x] `--json` — print events to stdout as JSONL (alternative output capture method)
- [x] `-m, --model <MODEL>` — specify model (can choose specific model within subscription)
- [x] `-C, --cd <DIR>` — set working directory
- [x] Auth file location — confirmed at `~/.codex/auth.json` (config at `~/.codex/config.toml`)

**Key insight from verification:** `--output-schema` is real and constrains the model's
final response shape. This is stronger than prompt-only JSON instructions and should be
used for all 9 JSON scenarios to improve reliability.

---

## 12 LLM Call Scenarios

| # | Scenario | Current Function | File | Output | Max Tokens |
|---|----------|-----------------|------|--------|------------|
| 1 | Resume parsing | `parse_resume_to_json()` | `services/parser.py:162` | JSON → `ResumeData` | 4096 |
| 2 | JD keyword extraction | `extract_job_keywords()` | `services/improver.py:530` | JSON → `JobKeywords` | 4096 |
| 3 | Resume improvement | `improve_resume()` | `services/improver.py:662` | JSON → `ResumeData` | 8192 |
| 4 | Diff-based improvement | `generate_resume_diffs()` | `services/improver.py:480` | JSON → `DiffResult` | 4096 |
| 5 | Keyword injection | `inject_keywords()` | `services/refiner.py:448` | JSON → `ResumeData` | 8192 |
| 6 | Resume analysis | `analyze_resume()` | `routers/enrichment.py:118` | JSON → `AnalysisPayload` | 8192 |
| 7 | Description enhancement | `generate_enhancements()` | `routers/enrichment.py:271` | JSON → `EnhancementPayload` | 4096 |
| 8 | Experience regeneration | `_regenerate_experience_or_project()` | `routers/enrichment.py:407` | JSON → `RegeneratedItem` | 4096 |
| 9 | Skills regeneration | `_regenerate_skills()` | `routers/enrichment.py:439` | JSON → `RegeneratedSkills` | 2048 |
| 10 | Cover letter | `generate_cover_letter()` | `services/cover_letter.py:38` | Plain text | 2048 |
| 11 | Outreach message | `generate_outreach_message()` | `services/cover_letter.py:70` | Plain text | 1024 |
| 12 | Resume title | `generate_resume_title()` | `services/cover_letter.py:99` | Plain text | 60 |

---

## Implementation

### Step 1: Create `codex_adapter.py`

New file: `apps/backend/app/codex_adapter.py`

This module must expose the **exact same signatures** as the current `llm.py` to avoid changing callers:

```python
# Must match llm.py signatures exactly (see llm.py:500-549, :748-853)
async def complete(
    prompt: str,
    system_prompt: str | None = None,
    config: LLMConfig | None = None,    # accepted but ignored (CLI uses subscription auth)
    max_tokens: int = 4096,             # accepted but ignored (CLI manages tokens)
    temperature: float = 0.7,           # accepted but ignored (CLI manages sampling)
) -> str

async def complete_json(
    prompt: str,
    system_prompt: str | None = None,
    config: LLMConfig | None = None,    # accepted but ignored
    max_tokens: int = 4096,             # accepted but ignored
    retries: int = 2,
) -> dict[str, Any]
```

> **Note:** `config`, `max_tokens`, `temperature` are accepted for signature compatibility
> but ignored — the CLI manages these internally via subscription. Callers like
> `generate_resume_title(temperature=0.3)` will work without changes.

> **Health/config functions** (`check_llm_health`, `get_llm_config`, `resolve_api_key`, `LLMConfig`)
> are NOT in this adapter — they always come from `llm.py` via the dispatch layer (Step 6).

#### Core subprocess invocation

```python
import asyncio
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from app.llm import LLMConfig, _appears_truncated, _extract_json

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
```

**Concurrency constraint:** `Semaphore(1)` means all LLM calls are globally sequential.
This is intentional for a single-user deployment:
- ChatGPT subscription has per-session rate limits
- CLI auth (`auth.json`) is designed for single-runner use
- 12 scenarios already have dependency chains (no benefit from parallelism)

If FastAPI receives concurrent requests, they queue behind the semaphore.
This makes the server effectively single-threaded for LLM work.

**Error type:** Raises `ValueError` (not `RuntimeError`) to match what callers expect
from the current `llm.py` — see `llm.py:547-549`.

### Step 2: JSON Reliability Pipeline

For the 9 JSON scenarios, raw CLI output goes through a multi-layer extraction and validation chain:

```
Prompt requires ```json wrapping
         ↓
   Output capture (file or stdout)
         ↓
  Code Fence extraction (preferred)
         ↓ fallback
  _extract_json from llm.py (brace-counting + thinking tag strip)
         ↓
   json.loads
         ↓
  Truncation check (_appears_truncated from llm.py)
         ↓ on failure
  Repair pass (send output + error back to CLI)
         ↓ still fails
  Bounded retry (max 2 generations + 1 repair)
         ↓ still fails
  Raise ValueError (callers already handle this)
```

> **JSON reliability:** `--output-schema <FILE>` is verified and available. When a JSON Schema
> file is provided, Codex validates the model's final response against it. This is analogous
> to LiteLLM's `response_format: json_object` but at the CLI level. Combined with the
> parsing pipeline below, this should provide strong JSON reliability.
>
> For each of the 9 JSON scenarios, export the Pydantic schema to a `.json` file:
> ```python
> # Generate schema files at build time or startup
> from app.schemas import ResumeData
> Path("schemas/resume_data.json").write_text(
>     json.dumps(ResumeData.model_json_schema(), indent=2)
> )
> ```
> Then pass `--output-schema schemas/resume_data.json` in `_run_codex()`.

#### 2a. Code Fence Extraction

```python
def _extract_from_code_fence(text: str) -> str | None:
    """Extract content from ```json ... ``` blocks."""
    match = re.search(r"```json\s*\n(.*?)\n\s*```", text, re.DOTALL)
    return match.group(1).strip() if match else None
```

#### 2b. Brace-counting Parser

`llm.py` already has `_extract_json()` (line 663-745) which does brace-counting
with thinking-tag stripping, markdown removal, truncation detection, and recursion limits.

```python
# Import from llm.py — copy into codex_adapter.py if you prefer no cross-module
# private imports. Both _extract_json and _appears_truncated are needed.
from app.llm import _extract_json, _appears_truncated
```

> **Alternative:** If importing private functions feels fragile, copy `_extract_json()`,
> `_strip_thinking_tags()`, and `_appears_truncated()` into `codex_adapter.py`.
> They have no external dependencies beyond stdlib + logging.

#### 2c. Repair Pass

```python
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
```

#### 2d. Complete JSON Flow

> **Important:** Current callers do NOT pass Pydantic schemas to `complete_json()`.
> The function signature must match `llm.py` exactly (no `schema` param).
> Pydantic validation happens in the **caller** (service layer), not in the adapter.
> The adapter's job is: get syntactically valid JSON dict. Schema validation is the caller's job.

```python
async def complete_json(
    prompt: str,
    system_prompt: str | None = None,
    config: LLMConfig | None = None,    # ignored — CLI uses subscription auth
    max_tokens: int = 4096,             # ignored — CLI manages tokens
    retries: int = 2,
) -> dict[str, Any]:
    """Make a completion request expecting JSON response.

    Raises ValueError on failure (matches llm.py behavior).
    """
    full_prompt = _build_json_prompt(system_prompt or "", prompt)

    # If a pre-exported schema file exists for this scenario, use it
    # for --output-schema constraint. Schema files are generated at startup
    # from Pydantic models (see JSON reliability note in Step 2).
    # Callers don't pass schema — the adapter resolves it from the prompt context.
    # For now, schema_path is None (prompt-only); add schema file mapping as enhancement.
    schema_path: str | None = None

    last_error = ""
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


def _try_parse(raw: str) -> tuple[dict[str, Any] | None, str]:
    """Attempt extraction → parse. Returns (result, error_msg).

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

    # Layer 4: Truncation check (reuse from llm.py)
    if _appears_truncated(data):
        return None, "JSON appears truncated (empty required arrays)"

    return data, ""
```

> **Latency note:** A single `complete_json` call may trigger up to `retries + 2` sequential
> CLI invocations (N generations + 1 repair). With default `retries=2`, that's up to 4 CLI calls.
> At ~15-60s per CLI call, worst case is ~4 minutes per `complete_json`.

> **Repair logic:** Repair runs AFTER all generation retries are exhausted, not mid-loop.
> Sequence: generate → generate → generate → repair. This avoids the ambiguity of
> mid-loop repair and ensures repair only fires as a last resort.

#### 2e. Prompt Template for JSON Scenarios

```python
def _build_json_prompt(system_prompt: str, user_prompt: str) -> str:
    """Build prompt that instructs CLI to output JSON.

    Note: The existing prompts in app/prompts/ already contain schema examples
    and JSON instructions. We add a reinforcing suffix, not a replacement.
    When --output-schema is also used, this serves as a belt-and-suspenders approach:
    prompt instructions + CLI-level schema validation.
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
```

### Step 3: Plain Text Flow

For the 3 text scenarios (cover letter, outreach message, title):

```python
async def complete(
    prompt: str,
    system_prompt: str | None = None,
    config: LLMConfig | None = None,    # ignored
    max_tokens: int = 4096,             # ignored
    temperature: float = 0.7,           # ignored
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
```

### Step 4: Wire Up — Replace `llm.py` Imports

All 7 files that import from `app.llm` need to switch to `app.llm_dispatch`:

```python
# Service files (complete / complete_json):
# Before:  from app.llm import complete_json
# After:   from app.llm_dispatch import complete_json

# Router files (health/config functions):
# Before:  from app.llm import check_llm_health, get_llm_config
# After:   from app.llm_dispatch import check_llm_health, get_llm_config
```

Files to update (**7 files**):

| File | Current imports |
|------|----------------|
| `services/parser.py` | `complete_json` |
| `services/improver.py` | `complete_json` |
| `services/refiner.py` | `complete_json` |
| `services/cover_letter.py` | `complete` |
| `routers/enrichment.py` | `complete_json` |
| `routers/config.py` | `check_llm_health`, `LLMConfig`, `resolve_api_key` |
| `routers/health.py` | `check_llm_health`, `get_llm_config` |

**No other changes needed.** All existing prompts, Pydantic schemas, and business logic remain untouched.

### Step 5: Docker Authentication

#### One-time setup (on host machine)

```bash
codex login
# Follow browser auth flow
# Creates ~/.codex/auth.json (verified)
```

> **Auth token expiry:** ChatGPT subscription tokens have TTLs (exact duration unknown —
> verify empirically). When the token expires, all CLI calls will fail with auth errors.
> There is no automated re-auth path inside a headless Docker container.
>
> **Monitoring:** Log all `ValueError` messages from `_run_codex()`. If you see auth-related
> errors (401, "unauthorized", "session expired"), the token needs manual refresh on the host.
> Consider adding a Prometheus counter or simple log alert for this.

#### Docker Compose mount

```yaml
services:
  backend:
    # ... existing config ...
    volumes:
      # Mount the host's Codex auth directory into the container.
      # VERIFY the actual path after running `codex login` on the host.
      # Use ${HOME} instead of ~ — tilde is NOT expanded by Docker Compose
      # in non-interactive contexts (CI, systemd, `docker compose up -d`).
      - ${HOME}/.codex:/root/.codex:rw
    environment:
      - CODEX_BIN=codex
      - CODEX_WORKDIR=/tmp/codex-runner
      - LLM_BACKEND=codex_cli
    healthcheck:
      # Availability check (verified: -V/--version flag exists)
      test: ["CMD", "codex", "--version"]
      interval: 60s
      timeout: 10s
      retries: 3
```

#### Dockerfile addition

```dockerfile
# Install Node.js (if not already in base image) and Codex CLI
RUN npm install -g @openai/codex
```

### Step 6: Configuration Switch

Add to `apps/backend/app/config.py`, inside the `Settings` class:

```python
# In the Settings class (follows existing snake_case convention):
llm_backend: Literal["litellm", "codex_cli"] = "litellm"
```

> Default is `"litellm"` (current behavior). Set `LLM_BACKEND=codex_cli` env var to switch.

Create a dispatch layer using lazy imports (avoids import-time issues with module-level conditionals):

```python
# apps/backend/app/llm_dispatch.py
"""Dispatch layer — routes complete/complete_json to the active backend.

Health/config functions always come from llm.py regardless of backend setting,
since they manage configuration and don't perform LLM inference.
"""
from typing import Any

from app.llm import (
    LLMConfig,
    check_llm_health,
    get_llm_config,
    resolve_api_key,
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
```

> **Why lazy imports:** Module-level conditional imports break hot reload, make testing harder,
> and can cause import errors that crash the entire app even when using the LiteLLM path.
> Lazy imports inside functions avoid all of these issues.

Then all service and router files import from `llm_dispatch` instead.

---

## Rollout Strategy

### Phase 1: Fixture Testing

1. Export Pydantic schemas to JSON Schema files
2. For each of the 12 scenarios, collect 3-5 real input/output pairs from current LiteLLM usage
3. Run same inputs through `codex_adapter` and compare outputs

**Pass criteria:**
- Parse success rate (including repair) >= 95%
- First-attempt Pydantic validation pass >= 90%
- Repair hit rate < 30% (if higher, tune prompts first)

### Phase 2: Canary by Scenario

Switch scenarios one at a time via feature flag:

```python
CODEX_ENABLED_SCENARIOS = {"generate_resume_title", "generate_cover_letter"}  # start simple
```

Start with the 3 text scenarios (simplest), then move to JSON scenarios in order of complexity.

### Phase 3: Full Cutover

Once all 12 scenarios pass fixture testing, remove the dispatch layer and LiteLLM dependency.

---

## Rate Limit Awareness

ChatGPT subscription plans have message caps (e.g., ~80 messages / 3 hours for Plus). One full resume tailoring flow uses ~12 CLI calls. This means:

- **~6 full flows per 3 hours** under Plus limits
- Single-user personal project: likely sufficient
- If cap is hit, CLI returns an error — the adapter surfaces this as a `ValueError`, not a silent failure

---

## Security Considerations

**Prompt injection via resume/JD content:** Resume data, job descriptions, and user text flow
into the prompt and then into the CLI subprocess via temp file → stdin. The Codex CLI is an
agentic tool — it can execute shell commands and file operations as part of its "coding agent"
behavior. A malicious resume containing instructions like "run `rm -rf /`" could theoretically
influence the agent's behavior.

Mitigations:
- `--sandbox read-only` (if verified) prevents file writes
- The existing prompt injection sanitization in `improver.py:47-55` (INJECTION_PATTERNS) strips
  known attack patterns before they reach the CLI
- Running in an isolated Docker container limits blast radius
- The workdir is an empty temp directory, not the project repo

> **Action item:** After verifying CLI flags, test with adversarial prompts to confirm
> sandbox enforcement.

---

## File Inventory

| Action | File | Description |
|--------|------|-------------|
| **Create** | `apps/backend/app/codex_adapter.py` | Core adapter: `_run_codex`, `complete`, `complete_json`, JSON pipeline |
| **Create** | `apps/backend/app/llm_dispatch.py` | Lazy dispatch between `codex_adapter` and `llm` |
| **Modify** | `apps/backend/app/config.py` | Add `llm_backend` setting to `Settings` class |
| **Modify** | `apps/backend/app/services/parser.py` | Change import `from app.llm` → `from app.llm_dispatch` |
| **Modify** | `apps/backend/app/services/improver.py` | Change import |
| **Modify** | `apps/backend/app/services/refiner.py` | Change import |
| **Modify** | `apps/backend/app/services/cover_letter.py` | Change import |
| **Modify** | `apps/backend/app/routers/enrichment.py` | Change import |
| **Modify** | `apps/backend/app/routers/config.py` | Change import (`check_llm_health`, `LLMConfig`, `resolve_api_key`) |
| **Modify** | `apps/backend/app/routers/health.py` | Change import (`check_llm_health`, `get_llm_config`) |
| **Modify** | `Dockerfile` | Install Codex CLI (`npm install -g @openai/codex`) |
| **Modify** | `docker-compose.yml` | Mount auth volume, add env vars, health check |

**Total: 2 new files, 10 modified files. Zero changes to business logic, prompts, or Pydantic schemas.**

---

## Review History

### Round 1 (self-review)
1. Signature mismatch fixed — Adapter signatures now exactly match `llm.py`
2. Missing exports fixed — Health/config functions routed through dispatch
3. No Pydantic in adapter — Validation stays in service layer
4. Reuse existing parser — `_extract_json()` from `llm.py`
5. Latency documented — Worst case 4 CLI calls per `complete_json`
6. File count corrected — 7 import sites + Dockerfile + docker-compose = 10 modified files

### Round 2 (devil's advocate review — 14 findings)
1. **[CRITICAL] CLI flags unverified** → Added Prerequisites section with verification checklist; marked all flags as placeholders in code
2. **[CRITICAL] stdin `-` unverified** → Switched to temp file for prompt input (safest method); stdin as secondary option
3. **[CRITICAL] `-o` output flag unverified** → Kept as placeholder with explicit fallback warning
4. **[CRITICAL] `complete()` signature inconsistency** → Fixed: Step 3 now uses explicit params matching `llm.py`, no `**kwargs`
5. **[CRITICAL] Module-level conditional import** → Replaced with lazy imports inside wrapper functions
6. **[MAJOR] Semaphore at module level** → Changed to lazy creation via `_get_semaphore()`
7. **[MAJOR] Semaphore(1) kills concurrency** → Added explicit documentation of single-user constraint and why
8. **[MAJOR] Repair pass off-by-one** → Restructured: repair now runs AFTER all retries, not mid-loop
9. **[MAJOR] `_appears_truncated` not imported** → Added explicit import alongside `_extract_json`
10. **[MAJOR] Prompt injection via CLI agent** → Added Security Considerations section
11. **[MAJOR] `LLM_BACKEND` naming convention** → Changed to `llm_backend` (lowercase) matching existing `Settings` fields
12. **[MAJOR] Docker health check command wrong** → Changed to `codex --version` (minimal availability check)
13. **[MODERATE] Auth token expiry** → Added monitoring note and manual refresh documentation
14. **[MODERATE] Double JSON instructions** → Added explicit note about instructional-only (no `response_format` guarantee)

### Round 3 (re-review — 3 new findings, all 14 originals confirmed fixed)
1. **[CRITICAL] Temp file written but never used** → Fixed: temp file opened as `stdin=prompt_file` file handle to `create_subprocess_exec` — no shell involvement
2. **[MAJOR] `_get_semaphore()` race condition caveat** → Fixed: added docstring explaining why this is safe in CPython asyncio (no await point = no interleaving)
3. **[MAJOR] Docker `~/.codex` tilde not expanded** → Fixed: changed to `${HOME}/.codex` with explanatory comment

### Round 4 (final review — 2 new findings from shell redirection)
1. **[CRITICAL] `create_subprocess_shell` reintroduces injection via env vars** → Fixed: reverted to `create_subprocess_exec`, pass temp file as `stdin=open(path, "rb")` file handle — no shell at all
2. **[MAJOR] CODEX_WORKDIR with special chars breaks shell quoting** → Fixed: same fix as above — no shell string construction involved
