# Codex CLI Migration — Implementation Plan

> Phased plan for replacing LiteLLM HTTP API calls with Codex CLI subprocess calls.
> See [codex-cli-migration.md](./codex-cli-migration.md) for the full technical design.

---

## Phase 1: Dispatch Layer (zero behavior change)

**Goal:** Wire up the routing infrastructure. Default backend stays `litellm` — app runs identically.

### Step 1: Add `llm_backend` setting

**File:** `apps/backend/app/config.py`

Add one field to the `Settings` class (after `log_llm` on line 130):

```python
llm_backend: Literal["litellm", "codex_cli"] = "litellm"
```

`Literal` import already exists on line 5. Default `"litellm"` preserves existing behavior.

### Step 2: Create `llm_dispatch.py`

**File:** `apps/backend/app/llm_dispatch.py` (new)

Lazy dispatch module that:
- Re-exports `check_llm_health`, `get_llm_config`, `resolve_api_key`, `LLMConfig` directly from `llm.py` (always)
- Routes `complete()` and `complete_json()` via lazy imports based on `settings.llm_backend`

See [migration doc, Step 6](./codex-cli-migration.md#step-6-configuration-switch) for full code.

### Step 3: Switch all 7 consumer files

Each file is a one-line import change:

| File | Line | Before | After |
|------|------|--------|-------|
| `services/parser.py` | 11 | `from app.llm import complete_json` | `from app.llm_dispatch import complete_json` |
| `services/improver.py` | 11 | `from app.llm import complete_json` | `from app.llm_dispatch import complete_json` |
| `services/refiner.py` | 17 | `from app.llm import complete_json` | `from app.llm_dispatch import complete_json` |
| `services/cover_letter.py` | 6 | `from app.llm import complete` | `from app.llm_dispatch import complete` |
| `routers/enrichment.py` | 14 | `from app.llm import complete_json` | `from app.llm_dispatch import complete_json` |
| `routers/config.py` | 10 | `from app.llm import check_llm_health, LLMConfig, resolve_api_key` | `from app.llm_dispatch import check_llm_health, LLMConfig, resolve_api_key` |
| `routers/health.py` | 6 | `from app.llm import check_llm_health, get_llm_config` | `from app.llm_dispatch import check_llm_health, get_llm_config` |

### Phase 1 Verification

- [ ] App starts normally with `LLM_BACKEND=litellm` (default)
- [ ] Existing integration tests pass (`pytest apps/backend/tests/`)
- [ ] `from app.llm_dispatch import complete, complete_json, check_llm_health` works in Python shell
- [ ] `LLM_BACKEND=codex_cli` causes `ImportError` at call time (expected — `codex_adapter.py` doesn't exist yet)

**Risk: Low** — mechanical changes, default preserves existing behavior.

---

## Phase 2: Codex Adapter Core

**Goal:** Create the adapter with full JSON reliability pipeline. Testable in isolation.

### Step 4: Create `codex_adapter.py` — subprocess + `complete()`

**File:** `apps/backend/app/codex_adapter.py` (new)

Implements:
- `_get_semaphore()` — lazy `Semaphore(1)` creation
- `_run_codex(prompt, schema_path=None)` — core subprocess invocation:
  - Writes prompt to temp file
  - Runs `codex exec --skip-git-repo-check --sandbox read-only --ephemeral -o <out_path> -`
  - `stdin=open(prompt_file, "rb")` via `asyncio.create_subprocess_exec` (no shell)
  - 300s timeout, temp file cleanup in `finally`
  - Optionally passes `--output-schema <schema_path>` for JSON scenarios
- `complete()` — matches `llm.py` signature exactly (accepts `config`, `max_tokens`, `temperature` but ignores them)

### Step 5: JSON pipeline + `complete_json()`

**File:** `apps/backend/app/codex_adapter.py` (continuing)

Implements:
- `_extract_from_code_fence(text)` — regex extraction from ` ```json ``` ` blocks
- `_try_parse(raw)` — pipeline: code fence → `_extract_json` (from `llm.py`) → `json.loads` → type check → `_appears_truncated` check
- `_repair_json(raw_output, error)` — sends malformed output + error back through `_run_codex`
- `_build_json_prompt(system_prompt, user_prompt)` — assembles prompt with JSON output instructions
- `complete_json()` — matches `llm.py` signature exactly. Retry loop + post-loop repair pass. Raises `ValueError` on failure.

Imports from `llm.py`: `_extract_json`, `_appears_truncated` (pure functions, ~100 lines total — copy into adapter if imports become fragile).

### Step 6: Unit tests

**Files (new):**
- `apps/backend/tests/unit/test_codex_adapter.py`
- `apps/backend/tests/unit/test_llm_dispatch.py`

Test coverage:
- `_extract_from_code_fence`: valid fences, nested fences, no fences, empty content
- `_try_parse`: valid JSON dict, valid JSON array (should reject), malformed JSON, truncated JSON, thinking tags
- `_build_json_prompt`: verify output structure
- `complete()` with mocked `_run_codex`: markdown stripping, empty response → `ValueError`
- `complete_json()` with mocked `_run_codex`: retry logic, repair invocation, final `ValueError`
- Dispatch routing: `litellm` routes to `llm.py`, `codex_cli` routes to `codex_adapter.py`

### Phase 2 Verification

- [ ] All new unit tests pass
- [ ] Manual smoke: `LLM_BACKEND=codex_cli`, call title generation (scenario 12 — simplest text)
- [ ] Manual smoke: call resume parsing (scenario 1 — JSON with complex schema)
- [ ] Verify `_run_codex` temp file cleanup works (no leaked files in `/tmp/codex-runner/`)

**Risk: Medium** — subprocess management, temp file cleanup, JSON parsing edge cases.

---

## Phase 3: Docker Infrastructure

**Goal:** Codex CLI available inside the container with auth.

### Step 7: Dockerfile changes

**File:** `Dockerfile`

After the Node.js binary copy (line 69), before `USER appuser` (line 106):

```dockerfile
# Copy npm for global installs
COPY --from=frontend-builder /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=frontend-builder /usr/local/bin/npm /usr/local/bin/npm

# Install Codex CLI
RUN npm install -g @openai/codex
```

### Step 8: docker-compose.yml changes

**File:** `docker-compose.yml`

Add to the `resume-matcher` service:

```yaml
volumes:
  - ${HOME}/.codex:/home/appuser/.codex:rw    # auth.json for Codex CLI
environment:
  - LLM_BACKEND=${LLM_BACKEND:-litellm}
  - CODEX_BIN=codex
  - CODEX_WORKDIR=/tmp/codex-runner
healthcheck:
  test: ["CMD", "codex", "--version"]          # verify CLI available
  interval: 60s
  timeout: 10s
  retries: 3
```

> **Note:** Container runs as `appuser` — mount target is `/home/appuser/.codex`, not `/root/.codex`. Verify after first test.

### Phase 3 Verification

- [ ] `docker build .` succeeds
- [ ] Inside container: `codex --version` returns version string
- [ ] Inside container: `ls /home/appuser/.codex/auth.json` shows auth file
- [ ] `LLM_BACKEND=codex_cli`: app starts, health endpoint responds
- [ ] `LLM_BACKEND=litellm`: app behaves identically to pre-migration

**Risk: Medium** — image size increase, auth mount path must match container user.

---

## Phase 4: Scenario-by-Scenario Validation

**Goal:** Validate all 12 scenarios through Codex CLI. No code changes — testing and prompt tuning only.

### Step 9: Text scenarios (3 scenarios, lowest risk)

| # | Scenario | Endpoint |
|---|----------|----------|
| 10 | Cover letter | `POST /api/v1/resumes/generate-cover-letter/{id}` |
| 11 | Outreach message | `POST /api/v1/resumes/generate-outreach/{id}` |
| 12 | Resume title | `GET /api/v1/resumes/{id}/tailor-details` |

Verify: non-empty text, no markdown artifacts, reasonable quality.

### Step 10: JSON scenarios (9 scenarios, by complexity)

Recommended validation order (simplest → most complex):

| Order | # | Scenario | Max Tokens |
|-------|---|----------|------------|
| 1 | 9 | Skills regeneration | 2048 |
| 2 | 8 | Experience regeneration | 4096 |
| 3 | 7 | Description enhancement | 4096 |
| 4 | 2 | JD keyword extraction | 4096 |
| 5 | 4 | Diff-based improvement | 4096 |
| 6 | 6 | Resume analysis | 8192 |
| 7 | 1 | Resume parsing | 4096 |
| 8 | 5 | Keyword injection | 8192 |
| 9 | 3 | Resume improvement | 8192 |

For each: verify `complete_json` returns valid dict, Pydantic validation passes in caller, check logs for repair rate.

### Phase 4 Verification

- [ ] All 12 scenarios produce correct output through Codex CLI
- [ ] Repair pass fires on fewer than 30% of JSON calls
- [ ] No auth token expiry during testing
- [ ] Full resume tailoring flow (parse → improve → refine) completes end-to-end

**Risk: High** — JSON reliability is the core migration risk. If repair rate > 30%, tune prompts before proceeding.

---

## Phase 5: Output Schema Enhancement (optional)

**Goal:** Improve JSON reliability using `--output-schema` flag.

### Step 11: Generate JSON Schema files from Pydantic models

**New directory:** `apps/backend/schemas/`

Export schemas at build time or startup:

| Schema File | Pydantic Model | Scenarios |
|-------------|---------------|-----------|
| `resume_data.json` | `ResumeData` | 1, 3, 5 |
| `job_keywords.json` | `JobKeywords` | 2 |
| `diff_result.json` | `DiffResult` | 4 |
| `analysis_payload.json` | `AnalysisPayload` | 6 |
| `enhancement_payload.json` | `EnhancementPayload` | 7 |
| `regenerated_item.json` | `RegeneratedItem` | 8 |
| `regenerated_skills.json` | `RegeneratedSkills` | 9 |

Add mapping in `complete_json` to select the right schema file.

### Phase 5 Verification

- [ ] `--output-schema` doesn't reject valid outputs (test with all 9 scenarios)
- [ ] Repair rate decreases compared to Phase 4 baseline
- [ ] If `--output-schema` causes false rejections, simplify schemas or revert to prompt-only

**Risk: Medium** — `--output-schema` behavior with complex nested schemas needs empirical validation.

---

## Phase 6: Cleanup (defer indefinitely)

**Goal:** Remove LiteLLM dependency once Codex CLI is proven stable.

### Step 12: Remove LiteLLM (only after 2+ weeks of stable production use)

- Remove `litellm` from `pyproject.toml`
- Remove `llm_dispatch.py` — callers import directly from `codex_adapter.py`
- Remove `llm_backend` config switch

**Risk: High** — irreversible. The dispatch layer exists so this step can be deferred forever.

---

## Risk Matrix

| Risk | Severity | Mitigation |
|------|----------|------------|
| CLI output format changes between versions | Medium | `-o` file capture + pin `@openai/codex` version in Dockerfile |
| Auth token expiry in headless Docker | Medium | Log monitoring + manual refresh docs |
| Rate limit (~80 msgs / 3 hours for Plus) | Low | Single-user sufficient, errors surface as `ValueError` |
| `--output-schema` rejects valid JSON | Low | Phase 5 optional, baseline doesn't depend on it |
| Private function imports from `llm.py` break | Low | Copy ~100 lines of pure functions if needed |
| `create_subprocess_exec` + file stdin behaves differently on Windows vs Linux | Low | Test both; temp file avoids platform-specific shell quoting |

---

## Success Criteria

- [ ] `LLM_BACKEND=litellm`: app identical to pre-migration (regression)
- [ ] `LLM_BACKEND=codex_cli`: all 3 text scenarios return correct text
- [ ] `LLM_BACKEND=codex_cli`: all 9 JSON scenarios return valid dicts passing Pydantic
- [ ] JSON repair rate < 30%
- [ ] Docker image builds with Codex CLI installed
- [ ] Auth mount works in container
- [ ] All existing + new tests pass
- [ ] Full resume tailoring flow completes end-to-end via frontend

---

## File Inventory

| Phase | Action | File |
|-------|--------|------|
| 1 | Modify | `apps/backend/app/config.py` |
| 1 | Create | `apps/backend/app/llm_dispatch.py` |
| 1 | Modify | `apps/backend/app/services/parser.py` |
| 1 | Modify | `apps/backend/app/services/improver.py` |
| 1 | Modify | `apps/backend/app/services/refiner.py` |
| 1 | Modify | `apps/backend/app/services/cover_letter.py` |
| 1 | Modify | `apps/backend/app/routers/enrichment.py` |
| 1 | Modify | `apps/backend/app/routers/config.py` |
| 1 | Modify | `apps/backend/app/routers/health.py` |
| 2 | Create | `apps/backend/app/codex_adapter.py` |
| 2 | Create | `apps/backend/tests/unit/test_codex_adapter.py` |
| 2 | Create | `apps/backend/tests/unit/test_llm_dispatch.py` |
| 3 | Modify | `Dockerfile` |
| 3 | Modify | `docker-compose.yml` |
| 5 | Create | `apps/backend/schemas/*.json` (7 files) |

**Total: 5 new source/test files, 10 modified files. Zero changes to business logic, prompts, or Pydantic schemas.**
