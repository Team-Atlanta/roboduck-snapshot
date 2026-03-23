# oss-crs Port — Known Gaps & TODOs

## Fixed: Infer Static Analysis

**Status**: Working — infer v1.1.0 runs inside DinD, compatible with base-runner (glibc 2.31)
**Changes**:
- `base.Dockerfile`: Downloads infer v1.1.0 from GitHub releases (AFTER `COPY ./external`); v1.1.0 chosen for glibc 2.31 compatibility (v1.2.0 needs 2.34+)
- `builder.Dockerfile`: Installs `bear` package for compile_commands.json generation
- `compile_target`: Wraps primary build with `bear -o "$SRC/compile_commands.json" compile` (bear 2.x syntax, no `--`)
- `run_roboduck.sh`: Tags `base-runner` as `$PROJECT:oss-crs` so roboduck's Docker calls find it
- `oss_crs_task.py`: Pre-creates bear tar from `/src`, build tars at correct `get_build_tar()` paths
- `static_analysis.py`: Removed `--no-bo-assume-void` flag (custom patch only, not in stock infer)
**Note**: Stock v1.1.0 lacks the original patches (p0-p2, macro.patch) for buffer overrun accuracy. Infer may return non-zero on initial run (normal) but retries with `--keep-going`.

## Fixed: Coverage/Debug Builds

**Status**: Working — build phase produces coverage and debug variants, caches pre-populated
**Changes**:
- `compile_target`: Runs 3 builds (primary, coverage, debug) and submits each as a named output
- `crs.yaml`: Declares `coverage-build` and `debug-build` outputs
- `run_roboduck.sh`: Downloads optional coverage/debug builds to `/out-coverage` and `/out-debug`
- `oss_crs_task.py`: Pre-populates `coverage_build_config` and `debug_build_config` caches with correct `get_build_tar()` paths
**Note**: Coverage and debug builds are best-effort — if `compile` fails for those configs, the system degrades gracefully.

## Fixed: DinD Storage Driver Performance

**Status**: fuse-overlayfs used instead of VFS — 2x+ more LLM analysis throughput
**Changes**:
- `base.Dockerfile`: Installs `fuse-overlayfs` package
- `run_roboduck.sh`: Tries overlay2 → fuse-overlayfs → vfs (fallback chain)
**Impact**: Container startup ~200x faster, LLM calls per run doubled (63 → 140 in 10min test).

## Skipped: Azure Blob Corpus Matching

**Status**: Disabled in oss-crs mode
**Location**: `crs/modules/fuzzing.py` — `match_corpus()` returns empty immediately when `ROBODUCK_MODE` is set
**Impact**: Fuzzers start with only the target's seed corpus (from `/out`), not the Azure-hosted pre-existing corpus collection. May reduce initial fuzzing effectiveness.

## Partially Implemented: Delta Mode

**Status**: E2E validated — DeltaTask created, fuzzing + triage + LLM analysis working
**Location**: `crs/app/oss_crs_task.py`, `oss-crs/scripts/inject_task.py`
**What works**: `inject_task.py` auto-detects delta mode from `$OSS_CRS_FETCH_DIR/diffs/ref.diff` (framework-standard). DeltaTask is created with the diff text. Fuzzing finds crashes, triage processes them, LLM agents analyze vulnerabilities. Base project build failure handled gracefully in `helpers.py`.
**Limitation**: Base project has no build artifacts, so `DeltaTask.test_pov_contents()` cannot verify regressions — all POVs are accepted. Proper base comparison requires builder sidecar integration (for pre-diff compilation).

## Performance: Docker Image Pulls Inside DinD

**Status**: Mitigated by fuse-overlayfs, but first-run pulls still slow
**Impact**: Roboduck pulls several Docker images inside DinD at runtime (base-runner, python_sandbox, joern). First pull is slow (~1-2min each). Subsequent runs benefit from Docker layer cache if data dir persists.

**Further mitigation options**:
1. Pre-load images into the DinD daemon during the prepare phase (e.g., `docker save | docker load`)
2. Include frequently-used images in the base image and `docker load` them at startup

## E2E Validation Status

**Status**: Full mode validated with mongoose target
**Date**: 2026-03-19
**Target**: `mongoose` / `fuzz`

**What works**:
- DinD with fuse-overlayfs (overlay2 → fuse-overlayfs fallback)
- Task injection via `inject_task.py`
- Infer static analysis (v1.1.0, bear + compile_commands.json)
- Coverage and debug build pre-population
- Fuzzer launch and seed production (140+ seeds in 10min)
- LiteLLM proxy integration (internal and external modes)
- LLM-based agents make tool calls (140 LLM calls in 10min)
- Seed submission via libCRS
- Joern CPG analysis uses bear tar

**What fails gracefully**:
- Ainalysis (LLM code analysis) — gtags `cannot stat` on mongoose source files (pre-existing VFS path mapping issue, not caused by oss-crs port)
- Base project builds in DeltaTask — logs warning, skips comparison

**Not yet validated**:
- POV submission end-to-end (needs a target with actual vulnerabilities)
- Delta mode with new build infrastructure

## Not Yet Tested: Unit Tests

**Status**: Written but not runnable locally
**Location**: `tests/app/test_oss_crs_submitter.py`, `tests/app/test_oss_crs_task.py`
**Impact**: Tests require full roboduck environment (crs_rust PyO3 extension, all pip deps). Can only run inside Docker or CI.
