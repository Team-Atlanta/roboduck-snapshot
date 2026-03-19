#!/bin/bash
# Run-phase entry point: set up environment, inject task, run roboduck.
set -e

cd /crs

###############################################################################
# 1. Start Docker daemon (DinD) — roboduck needs Docker for running fuzzers,
#    debugger, coverage, and PoV testing
###############################################################################
echo "[roboduck] Starting Docker daemon..."
# Try overlay2 first; if it fails (common in DinD), retry with vfs
_start_dockerd() {
    dockerd --host=unix:///var/run/docker.sock \
            --host=tcp://0.0.0.0:2375 \
            --tls=false \
            --log-level=warn \
            --storage-driver="$1" &
    DOCKERD_PID=$!
}

_start_dockerd overlay2

# Wait up to 10s for Docker daemon to come up
DOCKER_READY=0
for i in $(seq 1 10); do
    if docker info >/dev/null 2>&1; then
        DOCKER_READY=1
        break
    fi
    # If dockerd exited, overlay2 failed
    if ! kill -0 $DOCKERD_PID 2>/dev/null; then
        echo "[roboduck] overlay2 failed, retrying with vfs..."
        _start_dockerd vfs
    fi
    sleep 1
done

if [ "$DOCKER_READY" = "0" ]; then
    # Final wait with vfs
    for i in $(seq 1 30); do
        if docker info >/dev/null 2>&1; then
            DOCKER_READY=1
            break
        fi
        echo "[roboduck] Waiting for Docker daemon..."
        sleep 1
    done
fi

if [ "$DOCKER_READY" = "0" ]; then
    echo "[roboduck] ERROR: Docker daemon failed to start"
    exit 1
fi
echo "[roboduck] Docker daemon ready."

export DOCKER_HOST=unix:///var/run/docker.sock

###############################################################################
# 2. Download build outputs from the build phase
###############################################################################
echo "[roboduck] Downloading build outputs..."
mkdir -p /out /src
libCRS download-build-output build /out
libCRS download-build-output src /src

###############################################################################
# 3. Pull the base-runner image for running fuzzers/PoVs
###############################################################################
# Use oss-fuzz base-runner instead of AIxCC-specific image
export CRS_RUNNER_IMAGE="${CRS_RUNNER_IMAGE:-gcr.io/oss-fuzz-base/base-runner}"
echo "[roboduck] Pulling runner image: $CRS_RUNNER_IMAGE ..."
docker pull "$CRS_RUNNER_IMAGE" || \
    echo "[roboduck] Warning: could not pull runner image, will try to continue"

###############################################################################
# 4. Register POV submission directory with libCRS
#    (seed submit/fetch is registered in CorpusManager.init() at runtime)
###############################################################################
mkdir -p /artifacts/povs
libCRS register-submit-dir pov /artifacts/povs &
SUBMIT_POV_PID=$!

###############################################################################
# 5. Configure environment
###############################################################################
export ROBODUCK_MODE=bug-finding
export CACHE_DIR="${CACHE_DIR:-/tmp/roboduck-cache}"
export DATA_DIR="${DATA_DIR:-/crs/data}"
export LOGS_DIR="${LOGS_DIR:-/crs/logs}"
export GIT_DISCOVERY_ACROSS_FILESYSTEM=1
# No Azure registry
unset CRS_REGISTRY_NAME CRS_REGISTRY_DOMAIN

mkdir -p "$CACHE_DIR" "$DATA_DIR" "$LOGS_DIR"

# LLM configuration: use oss-crs LiteLLM proxy if available
if [ -n "$OSS_CRS_LLM_API_URL" ]; then
    export OSS_CRS_LLM_MODE=1
fi

# Use oss-crs model map if no MODEL_MAP is set
if [ -z "$MODEL_MAP" ]; then
    export MODEL_MAP=/crs/configs/models-oss-crs.toml
fi

###############################################################################
# 6. Inject the oss-crs task into roboduck's TaskDB
###############################################################################
echo "[roboduck] Injecting task..."
source /crs/.venv/bin/activate
python3 /opt/roboduck-oss-crs/inject_task.py

###############################################################################
# 7. Start roboduck
###############################################################################
echo "[roboduck] Starting roboduck main loop..."
# Start the task server (needed for internal polling)
python -m uvicorn --host 127.0.0.1 --port 1324 crs.task_server.app:app &
TASK_SERVER_PID=$!

# Run main CRS loop
python3 main.py

# Cleanup
kill $SUBMIT_POV_PID $TASK_SERVER_PID 2>/dev/null || true
