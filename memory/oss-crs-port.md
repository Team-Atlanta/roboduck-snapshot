# oss-crs Port Session Log

## What Was Done (2026-03-05)

### Adapter Layer Created (`oss-crs/`)
- `crs.yaml` — oss-crs phase definitions
- `docker-bake.hcl` — image build with `tags()` function for proper tagging
- `dockerfiles/base.Dockerfile` — roboduck base image (infer skipped, llvm-cov from source)
- `dockerfiles/builder.Dockerfile` — target compilation via oss-fuzz `compile`
- `dockerfiles/runner.Dockerfile` — `FROM roboduck-base`, runs main loop
- `scripts/run_roboduck.sh` — DinD startup (overlay2 → vfs fallback), build download, task injection
- `scripts/compile_target` — build phase entry point
- `scripts/inject_task.py` — converts oss-crs env vars to TaskDB entry

### Roboduck Code Changes
| File | Change |
|------|--------|
| `crs/__init__.py` | Relaxed Python version check: accept any 3.13.x (was <=3.13.4) |
| `crs/app/oss_crs_task.py` | NEW — creates Project from local /src + /out, discovers harness source files |
| `crs/app/oss_crs_submitter.py` | NEW — filesystem-based POV/seed/patch submission via `libCRS register-submit-dir` |
| `crs/config.py` | Added `OSS_CRS_LLM_API_URL` → litellm proxy routing |
| `crs/modules/search_code.py:170-174` | Guard empty `harness.source` in `get_clang_def_sites()` |
| `crs/modules/project.py` | `_build()` returns Err in oss-crs mode for non-cached configs |
| `crs/modules/fuzzing.py` | `match_corpus()` returns empty in oss-crs mode |
| `crs/app/app.py` | `loop()` uses `oss_crs_to_task()` when `ROBODUCK_MODE` is set |
| `configs/models-oss-crs.toml` | Model map for oss-crs (all Anthropic, includes FullMode/FullModeMulti) |

### Example Config
- `oss-crs/example/compose.yaml` — local dev compose with `source.local_path`
- `oss-crs/example/litellm-config.yaml` — claude-sonnet-4-6 + claude-haiku-4-5

## Key Debugging Discoveries

### DinD Storage Driver
- `overlay2` fails inside oss-crs containers → must fallback to `vfs`
- Fix: try overlay2 first, detect process exit, retry with vfs
- Location: `oss-crs/scripts/run_roboduck.sh`

### Python Version Gating
- `crs/__init__.py` monkeypatches CPython asyncio bug (cpython#130141)
- Has SHA256 hash check on `selector_events.py` — only pass for known good versions
- For 3.13.x the hash check is skipped (any 3.13 accepted)

### Docker Image Tags
- `docker-bake.hcl` must use `tags()` function to tag images as `roboduck-base:latest`
- Without tags, `FROM roboduck-base` in runner.Dockerfile fails

### Model Map Keys
- `FullMode` and `FullModeMulti` are required for `LAUNCH_AINALYSIS` pipeline
- These map to LLM models used for code vulnerability analysis
- Missing from initial `models-oss-crs.toml`, caused silent failure

### Harness Source Discovery
- `harness.source` must be a relative path to the source file in /src (e.g., `fuzz/fuzz_foo.c`)
- Empty string causes `IndexError` in `search_code.py:174` (`Path("").parts[0]`)
- Added `_find_harness_source()` in `oss_crs_task.py` that walks /src for matching filenames

## E2E Test Results
- **Target**: sanity-mock-c-delta-01 / fuzz_process_input_header
- **Result**: 55 crashes found in ~30s, triage pipeline started
- **Bottleneck**: Docker image pulls inside DinD with vfs driver (~3min for base-runner, python_sandbox, joern)
- **Not validated**: POV submission (timeout before triage completed)

## E2E Test Run #2 (2026-03-06)

### Setup
```bash
cd ~/oss-crs
COMPOSE=/home/hanqing/agents/CRSes/aixcc-teams/roboduck/oss-crs/example/compose.yaml
TARGET=~/oss-fuzz/projects/aixcc/c/sanity-mock-c-delta-01
SOURCE=/home/hanqing/agents/benchmarks/source_code/mock-c

uv run oss-crs prepare --compose-file $COMPOSE
uv run oss-crs build-target --compose-file $COMPOSE --fuzz-proj-path $TARGET --target-source-path $SOURCE
ANTHROPIC_API_KEY="..." uv run oss-crs run --compose-file $COMPOSE --fuzz-proj-path $TARGET --target-source-path $SOURCE --target-harness fuzz_process_input_header --timeout 600
```

### Results
- **prepare**: SUCCESS
- **build-target**: SUCCESS (requires `--target-source-path` for AIxCC projects with private `main_repo`)
- **run**: TIMEOUT after 600s, exit 1

### What Worked
- DinD startup (overlay2→vfs fallback, ~3s)
- Image pulls (base-runner ~90s, python_sandbox, joern)
- Task injection + project creation from /src + /out
- Harness source discovery (`fuzz/fuzz_process_input_header.c`)
- CRS main loop + FastAPI task_server
- LLM calls via litellm → claude-sonnet-4-6 (multiple successful calls)
- Agent tool calls: find_references, read_definition, read_source, gdb_exec, get_output
- HarnessInputEncoderAgent created and ran
- Fuzzing: found multiple crashes

### What Failed
| Issue | Location | Severity |
|-------|----------|----------|
| `ExceptionGroup` in fuzzing TaskGroup | `fuzzing.py:1005` | HIGH — worker crash, auto-restarts but loses state |
| `timeout waiting for container spawn` | `docker.py:307` | HIGH — vfs slow, base-runner spawn times out |
| `failed to write vfs: failed to mkdir at /src` | `docker.py:495` | HIGH — Joern CPG build fails |
| POV submission empty | SUBMIT_DIR/povs/ | HIGH — triage→POV→submit pipeline never completes |
| `model claude-sonnet-4-6 missing from concurrency!` | `llm_api.py:386` | MEDIUM — concurrency config |
| `LAUNCH_INFER` fails (bear build) | `project.py:771` | LOW — known oss-crs limitation |
| `debug/coverage build` fails | `debugger.py:186`, `coverage.py:332` | LOW — no rebuild in oss-crs |

## Fixes Applied (2026-03-06 session 2)

### 1. Concurrency config for claude-sonnet-4-6 (llm_api.py)
- Added `claude-sonnet-4-6` → `claude-4-sonnet` and `claude-opus-4-6` → `claude-4-opus` to `DUPE_MODEL_MAP`
- Result: Warning gone ✅

### 2. Background TaskGroup crash (fuzzing.py)
- Changed `asyncio.TaskGroup()` → `ExceptAndLogTaskGroup()` for `background_taskgroup` at line 838
- Result: `ExceptionGroup` crash eliminated (0 occurrences vs 1 before) ✅

### 3. Empty cid handling (docker.py + joern.py)
- Changed `if cid is None:` → `if not cid:` in `docker.py:317` to catch empty string cid
- Added `except RuntimeError` handlers in `joern.py` build_cpg and run_query
- Result: Joern container starts with proper cid, vwrite_layers succeeds, joern-parse runs ✅

## E2E Test Run #3 (2026-03-06, post-fix)
- ExceptionGroup: **0** (was 1) ✅
- Concurrency warning: **0** (was many) ✅
- Joern CPG build: **started successfully** (was failing) ✅
- Crashes found: 4
- POV submitted: 0 (pipeline didn't complete in 10min)
- AINALYSIS: failed (no results, separate issue)

### Remaining Issues
1. **DinD/vfs container startup is very slow** (~3min for base-runner) — biggest bottleneck
   - Consider pre-pulling images during prepare phase, or baking them into the runner image
2. **POV pipeline not completing** — needs longer timeout or faster container starts to validate

## Delta Mode Implementation (2026-03-06, session 3)

### Changes
1. **`oss-crs/scripts/inject_task.py`** — Auto-detect delta mode from `$OSS_CRS_FETCH_DIR/diffs/ref.diff` (framework-standard). Removed custom `OSS_CRS_TARGET_MODE` and `OSS_CRS_DIFF_PATH` env vars.
2. **`crs/app/oss_crs_task.py`** — DeltaTask creation with diff from `_read_diff_from_sources()`. Base project has no builds → POV comparison skipped.
3. **`crs/app/helpers.py`** — BulkCrashWorker handles base_proj.build_all() Err gracefully instead of crashing.
4. **`crs/agents/classifier.py`** — Non-GPT model support: text-based fallback when logprobs unavailable (was hardcoding gpt-4o-mini fallback).
5. **Patching disabled** — 3-level defense in `app.py`: skip callback registration, skip PATCH_VULN job, early-return in schedule_new_patcher.

### oss-crs Framework Bug: Double build_id Normalization
- `run()` normalizes build_id at line 481, then `build_target()` normalizes again at line 328
- Build artifacts end up at double-normalized path but run mounts single-normalized path
- **Fix applied** in `~/oss-crs/oss_crs/src/crs_compose.py`: after `build_target()` returns, re-derive actual build_id via `get_latest_build_id()`
- Only affects `run` with `--diff` triggering inline build (separate build-target + run is fine)
- Also: `build-target --diff` CLI crashes with `AssertionError: target_harness must be set` because `build-target` doesn't have `--target-harness` flag. Use `run --diff` instead (which has `--target-harness` and auto-builds).

### Delta Mode E2E Test Results
- **Command**: `oss-crs run --diff ref.diff --target-harness fuzz_process_input_header --build-id delta-test-06 --timeout 600`
- **DeltaTask created**: ✅ "Created DeltaTask with 1331 byte diff"
- **Task type "delta"**: ✅ Verified in oss_crs_task.json
- **Fuzzing**: ✅ 51+ crashes found
- **Base project build**: ✅ Warning logged, comparison skipped (no crash)
- **LLM triage agents**: ✅ Claude-sonnet-4-6 making tool calls (read_source, list_definitions)
- **Classifier**: ✅ Falls back to text parsing (was crashing with gpt-4o-mini not found)
- **Vuln analysis**: ✅ `handle_analyzed_vuln: submitting 1 new jobs for vuln_id=1..4`
- **POV submission**: ❌ 0 POVs in final artifacts (pipeline runs but doesn't complete in 10min)

### Key Learnings
- AIxCC projects with private `main_repo` in project.yaml need `--target-source-path`
- `oss-crs run --diff` is the correct flow for delta mode (not separate build-target + run)
- Classifier logprobs are OpenAI-only; Claude needs text-based fallback
- The oss-crs framework double-normalizes build_id when run() triggers inline build_target()
