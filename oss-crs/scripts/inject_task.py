#!/usr/bin/env python3
"""
Inject a synthetic task into roboduck's TaskDB from oss-crs environment variables.

This replaces the HTTP API task submission flow (api_to_crs_task) for oss-crs mode.
Instead of downloading tarballs from URLs, it creates TaskDetail records that point
to the pre-built artifacts and source code already on disk.

Environment variables read:
  OSS_CRS_TARGET          — target project name (e.g. "libxml2")
  OSS_CRS_TARGET_HARNESS  — harness binary name (e.g. "xml")
  FUZZING_LANGUAGE         — language (c, c++, jvm, ...)
  SANITIZER                — sanitizer (address, memory, ...)
  ARCHITECTURE             — architecture (x86_64)
  FUZZING_ENGINE           — engine (libfuzzer)
  OSS_CRS_CPUSET           — allocated CPU cores
  OSS_CRS_MEMORY_LIMIT     — memory limit

Files on disk (from build phase):
  /out   — compiled harness binaries + build artifacts
  /src   — source tree
"""
import asyncio
import json
import os
import sys
import time
import uuid

# Add the CRS root to Python path
sys.path.insert(0, "/crs")

from crs import config
from crs.task_server.db import TaskDB
from crs.task_server.models import Task, TaskDetail, TaskType, SourceDetail, SourceType


def make_task_detail() -> TaskDetail:
    """Create a TaskDetail from oss-crs environment variables."""
    target = os.environ.get("OSS_CRS_TARGET", "unknown")
    harness = os.environ.get("OSS_CRS_TARGET_HARNESS", "")
    task_type_str = os.environ.get("OSS_CRS_TARGET_MODE", "full")

    task_type = TaskType.TaskTypeDelta if task_type_str == "delta" else TaskType.TaskTypeFull

    # Deadline: use timeout from env or default to 4 hours from now
    timeout_s = int(os.environ.get("OSS_CRS_TIMEOUT", str(4 * 3600)))
    deadline_ms = int((time.time() + timeout_s) * 1000)

    task_id = uuid.uuid4()

    # We don't have real source URLs — we use placeholder "file://" URLs.
    # The actual source and build outputs are at /src and /out on disk.
    # api_to_crs_task won't be called; instead CRS._task_from_id() will
    # detect oss-crs mode and build the Task object from local paths.
    sources = [
        SourceDetail(
            type=SourceType.SourceTypeRepo,
            url="file:///src",
            sha256="oss-crs-local",
        ),
        SourceDetail(
            type=SourceType.SourceTypeFuzzTooling,
            url="file:///oss-fuzz-project",
            sha256="oss-crs-local",
        ),
    ]

    # If there's a diff file, add it as a source
    diff_path = os.environ.get("OSS_CRS_DIFF_PATH")
    if diff_path and os.path.exists(diff_path):
        sources.append(SourceDetail(
            type=SourceType.SourceTypeDiff,
            url=f"file://{diff_path}",
            sha256="oss-crs-local-diff",
        ))

    metadata = {
        "oss_crs": "true",
        "target": target,
        "harness": harness,
        "language": os.environ.get("FUZZING_LANGUAGE", "c++"),
        "sanitizer": os.environ.get("SANITIZER", "address"),
    }

    return TaskDetail(
        task_id=task_id,
        type=task_type,
        project_name=target,
        focus=target,
        harnesses_included=True,
        deadline=deadline_ms,
        source=sources,
        metadata=metadata,
    )


async def main():
    os.makedirs(config.DATA_DIR, exist_ok=True)

    taskdb = TaskDB()
    task_detail = make_task_detail()

    task = Task(
        message_id=uuid.uuid4(),
        message_time=int(time.time() * 1000),
        tasks=[task_detail],
    )

    await taskdb.put_tasks(task)

    # Write task info to a file for the run script to reference
    info = {
        "task_id": str(task_detail.task_id),
        "project_name": task_detail.project_name,
        "deadline": task_detail.deadline,
        "type": task_detail.type.value,
    }
    info_path = os.path.join(str(config.DATA_DIR), "oss_crs_task.json")
    with open(info_path, "w") as f:
        json.dump(info, f, indent=2)

    print(f"[inject_task] Injected task {task_detail.task_id} for project '{task_detail.project_name}'")
    print(f"[inject_task] Task info written to {info_path}")


if __name__ == "__main__":
    asyncio.run(main())
