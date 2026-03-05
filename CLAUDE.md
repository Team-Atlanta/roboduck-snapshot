# Roboduck CRS

## Overview
Roboduck is Team Atlanta's Cyber Reasoning System (CRS) for autonomous vulnerability discovery and patching. It uses multi-agent LLM orchestration combined with fuzzing, static analysis, and dynamic debugging.

## Architecture

### Entry Points
- `main.py` → `crs.app.app.CRS().loop()` — main async event loop
- `run-crs.sh` — spawns both the CRS loop and the task_server API (FastAPI on port 1324)
- `round_sim.py` — simulation tool that sends tasks via API with timing control

### Core Directories
| Directory | Purpose |
|-----------|---------|
| `crs/app/` | Main CRS orchestration, work queue (WorkDB), submission |
| `crs/agents/` | LLM-powered agents (vuln analysis, patching, POV production, triage, diff analysis) |
| `crs/modules/` | Infrastructure: project build, fuzzing, coverage, debugger, static analysis, code search |
| `crs/analysis/` | LLM-based and tree-sitter code analysis |
| `crs/common/` | Shared types, LLM API wrapper, Docker utilities, async helpers |
| `crs/task_server/` | FastAPI task intake API |
| `src/` | Rust code (PyO3 bindings) for logging, metrics, patching, HTTP |
| `external/` | Infer, llvm-cov, bear, corpus |
| `configs/` | Model mapping TOML files (agent → LLM model) |
| `prompts/` | LLM prompt templates |
| `projects/` | Test target projects (oss-fuzz format) |

### Bug-Finding Pipeline
```
Task → LAUNCH_BUILDS → parallel:
  ├─ LAUNCH_FUZZERS (LibFuzzer/AFL/Honggfuzz)
  ├─ LAUNCH_INFER (Facebook Infer static analysis)
  ├─ LAUNCH_AINALYSIS (LLM code analysis)
  └─ ANALYZE_DIFF (delta mode)
    → VulnReport → SCORE_VULN → ANALYZE_VULN → PRODUCE_POV
```

### Bug-Fixing Pipeline
```
AnalyzedVuln + POV → TRIAGE_POV → PATCH_VULN → test patch → BUNDLE → SUBMIT
```

### Configuration
- `MODEL_MAP` env → TOML file mapping agent class names to LLM models
- `MODEL` / `SMALLMODEL` env → default LLM models
- API keys: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `AZURE_API_KEY`
- `CAPI_URL/CAPI_ID/CAPI_TOKEN` — Competition API (AIxCC)
- `CACHE_DIR`, `LOG_LEVEL`, `DATA_DIR`, `LOGS_DIR`

### Docker
- `Dockerfile` — Ubuntu 24.04 base with Python 3.13, Rust, Java 17, Infer, LLVM tools
- `docker-compose.yml` — DinD setup with crs-init, docker-daemon, crs services
- Needs Docker-in-Docker for building/running target containers internally

## OSS-CRS Integration (WIP)
Target: port roboduck to the [oss-crs](https://github.com/sslab-gatech/oss-crs) framework.

### oss-crs Key Concepts
- **Three phases**: prepare (build CRS images), build-target (compile target), run (launch CRS)
- **crs.yaml**: defines phases, Dockerfiles, supported targets, required LLMs
- **libCRS**: CLI library in every container for build output exchange, artifact submission, inter-module communication
- **Environment variables**: `OSS_CRS_*` vars provide target info, paths, resource limits
- **LLM access**: via `OSS_CRS_LLM_API_URL` / `OSS_CRS_LLM_API_KEY` (LiteLLM proxy)
- **Artifacts**: submit via `libCRS submit <type> <path>` or `libCRS register-submit-dir`
- **Builder sidecar**: for incremental rebuild via `libCRS apply-patch-build`, `run-pov`, `run-test`

### Known Gaps
See [oss-crs/TODO.md](oss-crs/TODO.md) for detailed tracking of skipped/stubbed features.

### Related Repos
- oss-crs framework: `~/oss-crs` (github.com/sslab-gatech/oss-crs)
- oss-fuzz targets: `~/oss-fuzz`
- Skill: `~/.claude/skills/run-oss-crs/`

## Session Memory

Session notes and debugging logs are stored in `memory/` directory within this repo. Always write session memory here (not in `~/.claude/memory`), and reference from this CLAUDE.md.

- [oss-crs-port.md](memory/oss-crs-port.md) — oss-crs adapter layer changes, debugging discoveries, E2E test results
