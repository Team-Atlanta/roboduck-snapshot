# oss-crs Port — Known Gaps & TODOs

## Skipped: Infer Static Analysis

**Status**: Temporarily skipped
**Location**: `oss-crs/dockerfiles/base.Dockerfile` (Stage 1 removed)
**Root cause**: Pinned infer commit (`1b1366e6`) depends on opam packages that no longer resolve against current opam repositories. Original roboduck pulled pre-built infer from Azure Blob Storage.

**Impact**:
- `LAUNCH_INFER` pipeline stage fails gracefully (no `infer` binary at `external/infer/infer/bin/infer`)
- No static analysis VulnReports from infer
- Bug-finding still works: fuzzing (`LAUNCH_FUZZERS`), LLM analysis (`LAUNCH_AINALYSIS`), diff analysis (`ANALYZE_DIFF`) are unaffected
- Affects code path: `crs/modules/infer.py`

**Fix options**:
1. Pin opam to an older repository snapshot (opam repo archive)
2. Update infer to a newer commit with compatible dependencies
3. Host pre-built infer binary somewhere accessible (GCS bucket, GitHub release)
4. Use the existing `external/infer/Dockerfile` to pre-build an infer image, then `COPY --from` that image

## Skipped: Azure Blob Corpus Matching

**Status**: Disabled in oss-crs mode
**Location**: `crs/modules/fuzzing.py` — `match_corpus()` returns empty immediately when `ROBODUCK_MODE` is set
**Impact**: Fuzzers start with only the target's seed corpus (from `/out`), not the Azure-hosted pre-existing corpus collection. May reduce initial fuzzing effectiveness.

## Partially Implemented: Delta Mode

**Status**: DeltaTask created, ANALYZE_DIFF works, base POV comparison disabled
**Location**: `crs/app/oss_crs_task.py`
**What works**: DeltaTask is created with the diff text from `OSS_CRS_DIFF_PATH`. The LLM-based `ANALYZE_DIFF` pipeline analyzes the diff for introduced vulnerabilities. Fuzzing also runs on the post-diff source.
**Limitation**: Base project has no build artifacts, so `DeltaTask.test_pov_contents()` cannot verify regressions — all POVs are accepted. Proper base comparison requires builder sidecar integration (for pre-diff compilation).

## Not Yet Implemented: Coverage/Debug Builds

**Status**: Blocked gracefully
**Location**: `crs/modules/project.py` — `_build()` returns `Err(BuildError(...))` when `ROBODUCK_MODE` is set and config is not pre-cached
**Impact**: Coverage-guided analysis and debug builds won't work. Only the primary build config (from the build phase) is available.

## Performance: Docker Image Pulls Inside DinD

**Status**: Working but slow
**Location**: `oss-crs/scripts/run_roboduck.sh`, `crs/common/docker.py`
**Impact**: Roboduck pulls several Docker images inside DinD at runtime (base-runner, python_sandbox, joern). With the `vfs` storage driver (DinD fallback), these pulls are slow (~2min each). A 5-minute timeout is insufficient for the full triage pipeline to complete.

**Mitigation options**:
1. Pre-load images into the DinD daemon during the prepare phase (e.g., `docker save | docker load`)
2. Include frequently-used images in the base image and `docker load` them at startup
3. Use longer timeouts (10+ minutes)
4. Skip joern analysis in oss-crs mode if not needed

## E2E Validation Status

**Status**: Fuzzing pipeline validated
**Date**: 2026-03-05
**Target**: `sanity-mock-c-delta-01` / `fuzz_process_input_header`

**What works**:
- DinD with vfs fallback
- Task injection via `inject_task.py`
- Harness source discovery (`fuzz/fuzz_process_input_header.c`)
- Fuzzer launch and crash discovery (55 crashes in ~30s)
- Triage pipeline starts processing POVs
- LiteLLM proxy integration

**What fails gracefully**:
- `LAUNCH_INFER` — no infer binary (documented above)
- Coverage/debug builds — only pre-built artifacts available
- Bear build — not available in oss-crs mode

**Not yet validated**:
- POV submission via OSSCRSSubmitter (triage didn't complete before timeout)
- LLM-based analysis (LAUNCH_AINALYSIS) — needs longer timeout
- Seed submission

## Not Yet Tested: Unit Tests

**Status**: Written but not runnable locally
**Location**: `tests/app/test_oss_crs_submitter.py`, `tests/app/test_oss_crs_task.py`
**Impact**: Tests require full roboduck environment (crs_rust PyO3 extension, all pip deps). Can only run inside Docker or CI.
