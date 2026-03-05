"""
Build a roboduck Task from oss-crs local paths.

In oss-crs mode, the build phase has already compiled the target and submitted
the artifacts. The source tree is at /src and build outputs are at /out.
This module creates a Project + Task without downloading any tarballs.
"""
import asyncio
import os
import pathlib
import tarfile

import yaml

from crs.common.aio import Path
from crs.common.types import Result, Ok, Err, CRSError, BuildConfig
from crs.common.vfs import EditableOverlayFS, TarFS
from crs.common.constants import DEFAULT_SANITIZER, DEFAULT_ARCHITECTURE, DEFAULT_ENGINE
from crs.modules import project
from crs.modules.project import (
    Project, ProjectInfo, BuildArtifacts, Harness, HarnessType,
)
from crs.modules.coverage import CoverageAnalyzer
from crs.modules.debugger import Debugger
from crs.task_server.models import TaskDetail, TaskType
from crs import config

from crs_rust import logger


def _find_harness_source(harness_name: str, src_dir: str = "/src") -> str:
    """
    Try to find the source file for a harness binary in the source tree.

    Looks for files matching the harness name (e.g., fuzz_foo.c, fuzz_foo.cc,
    fuzz_foo.cpp) or files containing LLVMFuzzerTestOneInput whose name matches.
    Returns a path relative to src_dir, or "" if not found.
    """
    extensions = [".c", ".cc", ".cpp", ".cxx", ".C"]
    for root, _dirs, files in os.walk(src_dir):
        for fname in files:
            stem = pathlib.Path(fname).stem
            if stem == harness_name and any(fname.endswith(ext) for ext in extensions):
                rel = os.path.relpath(os.path.join(root, fname), src_dir)
                return rel
    return ""


async def _create_project_from_local(task_detail: TaskDetail) -> Project:
    """
    Create a Project from local /src and /out directories.

    Instead of building via Docker (the normal flow), we:
    1. Create a project.yaml from env vars
    2. Tar the source into a VFS
    3. Pre-populate BuildArtifacts from /out
    """
    project_name = task_detail.project_name
    language = os.environ.get("FUZZING_LANGUAGE", "c++")
    sanitizer = os.environ.get("SANITIZER", DEFAULT_SANITIZER)
    architecture = os.environ.get("ARCHITECTURE", DEFAULT_ARCHITECTURE)
    engine = os.environ.get("FUZZING_ENGINE", DEFAULT_ENGINE)
    harness_name = os.environ.get("OSS_CRS_TARGET_HARNESS", "")

    # Create the oss-fuzz project directory layout
    project_dir = Path(config.CACHE_DIR) / "oss-crs-project" / project_name
    os.makedirs(project_dir, exist_ok=True)

    # Write project.yaml
    project_yaml_path = project_dir / "project.yaml"
    project_yaml = {
        "main_repo": f"https://github.com/placeholder/{project_name}",
        "language": language,
        "sanitizers": [sanitizer],
        "architectures": [architecture],
        "fuzzing_engines": [engine],
    }
    with open(project_yaml_path, "w") as f:
        yaml.dump(project_yaml, f)

    info = ProjectInfo(**project_yaml)

    # Create data directory
    data_dir = Path(config.CACHE_DIR) / "data" / "oss-crs" / project_name
    os.makedirs(data_dir, exist_ok=True)

    # Create source VFS from /src
    src_tar_path = data_dir / "src.tar"
    if not os.path.exists(src_tar_path):
        logger.info(f"Creating source tar from /src -> {src_tar_path}")
        def _tar_src():
            with tarfile.open(src_tar_path, "w") as tf:
                tf.add("/src", arcname=".")
        await asyncio.to_thread(_tar_src)
    src_vfs = EditableOverlayFS(await TarFS.fsopen(src_tar_path))

    # Create build artifacts VFS from /out
    build_config = BuildConfig(
        FUZZING_LANGUAGE=language,
        SANITIZER=sanitizer,
        ARCHITECTURE=architecture,
        FUZZING_ENGINE=engine,
    )
    build_tar_path = data_dir / f"build_{build_config}.tar"
    if not os.path.exists(build_tar_path):
        logger.info(f"Creating build tar from /out -> {build_tar_path}")
        def _tar_out():
            with tarfile.open(build_tar_path, "w") as tf:
                tf.add("/out", arcname=".")
        await asyncio.to_thread(_tar_out)
    build_vfs = await TarFS.fsopen(build_tar_path)
    build_artifacts = BuildArtifacts(project_name, build_config, build_vfs)

    # Create Project
    proj = Project(
        project_dir=project_dir,
        data_dir=data_dir,
        vfs=src_vfs,
        info=info,
        ossfuzz_hash="oss-crs",
        workdir="/src",
    )

    # Pre-populate builds so build_all() returns immediately
    state = await proj.edit_state()
    proj.builds[state][build_config] = build_artifacts

    # Pre-populate harness info if we know the harness name
    if harness_name:
        source = _find_harness_source(harness_name)
        if source:
            logger.info(f"Found harness source for {harness_name}: {source}")
        else:
            logger.warning(f"Could not find source file for harness {harness_name}")
        harness = Harness(
            name=harness_name,
            type=HarnessType.LIBFUZZER,
            source=source,
            options="",
            harness_func="LLVMFuzzerTestOneInput",
        )
        proj.harnesses = [harness]
    else:
        # Try to discover harnesses from /out
        harnesses = []
        if os.path.isdir("/out"):
            for f in sorted(os.listdir("/out")):
                fpath = os.path.join("/out", f)
                if os.path.isfile(fpath) and os.access(fpath, os.X_OK):
                    # Skip non-harness files
                    if f.endswith((".options", ".dict", ".labels", ".cfg")):
                        continue
                    if f.endswith("_seed_corpus.zip"):
                        continue
                    if "." in f:  # skip files with extensions (libraries, etc.)
                        continue
                    options = ""
                    opt_path = os.path.join("/out", f"{f}.options")
                    if os.path.exists(opt_path):
                        with open(opt_path) as of:
                            options = of.read()
                    source = _find_harness_source(f)
                    harnesses.append(Harness(
                        name=f,
                        type=HarnessType.LIBFUZZER,
                        source=source,
                        options=options,
                        harness_func="LLVMFuzzerTestOneInput",
                    ))
        if harnesses:
            proj.harnesses = harnesses
            logger.info(f"Discovered {len(harnesses)} harnesses from /out: "
                       f"{[h.name for h in harnesses]}")

    return proj


async def oss_crs_to_task(task_detail: TaskDetail) -> Result[project.Task]:
    """
    Convert a TaskDetail (injected by inject_task.py) into a roboduck Task.
    This is the oss-crs equivalent of api_to_crs_task().
    """
    try:
        proj = await _create_project_from_local(task_detail)

        # NOTE: DeltaTask requires a proper base project (pre-patch version) to
        # compare POV results. In oss-crs mode, we don't have the base project
        # yet (would need the builder sidecar to produce both pre/post builds).
        # For now, always use FullTask. Delta support will be added later when
        # we integrate the oss-crs builder sidecar (snapshot mode).
        if task_detail.type == TaskType.TaskTypeDelta:
            logger.warning("Delta task requested in oss-crs mode; "
                         "running as full task (delta not yet supported)")

        return Ok(project.Task(
            task_detail.task_id,
            task_detail.deadline,
            proj,
            CoverageAnalyzer(proj),
            Debugger(proj),
            task_detail.metadata,
        ))
    except Exception as e:
        logger.exception(f"Failed to create oss-crs task: {e}")
        return Err(CRSError(f"failed to create oss-crs task: {e}"))
