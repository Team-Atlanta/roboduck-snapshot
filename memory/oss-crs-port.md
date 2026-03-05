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

## How to Run E2E Test
```bash
cd ~/oss-crs
uv run oss-crs prepare --compose-file /path/to/roboduck/oss-crs/example/compose.yaml
uv run oss-crs build-target --compose-file ... --fuzz-proj-path ~/oss-fuzz/projects/aixcc/c/sanity-mock-c-delta-01
ANTHROPIC_API_KEY="..." uv run oss-crs run --compose-file ... --fuzz-proj-path ... --target-harness fuzz_process_input_header --timeout 600
```
