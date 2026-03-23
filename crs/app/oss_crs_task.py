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
from crs.task_server.models import TaskDetail, TaskType, SourceType
from crs.app.api_task import rewrite_diff
from crs import config

from crs_rust import logger


async def _populate_optional_build(
    proj: Project,
    state: str,
    build_config: "BuildConfig",
    out_dir: str,
    label: str,
) -> None:
    """Pre-populate an optional build config (coverage/debug) from a local directory."""
    if not os.path.isdir(out_dir) or not os.listdir(out_dir):
        logger.info(f"No {label} build available at {out_dir}, skipping.")
        return

    # Use proj.get_build_tar() to get the path that get_build_vfs() expects
    tar_path = await proj.get_build_tar(build_config)
    if not os.path.exists(tar_path):
        logger.info(f"Creating {label} build tar from {out_dir} -> {tar_path}")
        def _tar():
            with tarfile.open(tar_path, "w") as tf:
                tf.add(out_dir, arcname=".")
        await asyncio.to_thread(_tar)

    vfs = await TarFS.fsopen(tar_path)
    artifacts = BuildArtifacts(proj.name, build_config, vfs)
    proj.builds[state][build_config] = artifacts
    logger.info(f"Pre-populated {label} build cache for config {build_config}")


def _find_project_workdir(project_name: str, src_dir: str = "/src") -> str:
    """
    Find the actual project root directory inside /src.

    oss-fuzz clones the main repo into /src/<repo-name>/. The build.sh
    typically does `cd $SRC/<name>`. We detect it by:
    1. Looking for /src/<project_name>/ (exact match)
    2. Looking for git repos one level deep in /src
    3. Falling back to /src
    """
    # Direct match by project name
    candidate = os.path.join(src_dir, project_name)
    if os.path.isdir(candidate):
        return candidate

    # Look for git repos one level deep
    for entry in sorted(os.listdir(src_dir)):
        entry_path = os.path.join(src_dir, entry)
        if os.path.isdir(entry_path) and os.path.isdir(os.path.join(entry_path, ".git")):
            return entry_path

    return src_dir


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
            with tarfile.open(src_tar_path, "w", dereference=True) as tf:
                tf.add("/src", arcname=".")
        await asyncio.to_thread(_tar_src)
    src_vfs = EditableOverlayFS(await TarFS.fsopen(src_tar_path))

    # Create build config
    build_config = BuildConfig(
        FUZZING_LANGUAGE=language,
        SANITIZER=sanitizer,
        ARCHITECTURE=architecture,
        FUZZING_ENGINE=engine,
    )

    # Discover the actual project working directory inside /src.
    # oss-fuzz clones the main repo into /src/<repo-name>/, so the workdir
    # is typically /src/<repo-name> (matching build.sh's "cd $SRC/<name>").
    workdir = _find_project_workdir(project_name)
    logger.info(f"Detected project workdir: {workdir}")

    # Create Project first — we need it to compute correct tar paths
    proj = Project(
        project_dir=project_dir,
        data_dir=data_dir,
        vfs=src_vfs,
        info=info,
        ossfuzz_hash="oss-crs",
        workdir=workdir,
    )

    # build_image defaults to "project:oss-crs" which is created in
    # run_roboduck.sh by importing the current container's filesystem into DinD.
    # This gives all Docker operations (ainalysis, gtags, infer) access to
    # Ubuntu 24.04 tools and glibc 2.39.

    # Create build tar from /out at the path get_build_tar() expects
    # (includes project name + edit_state hash in filename)
    build_tar_path = await proj.get_build_tar(build_config)
    if not os.path.exists(build_tar_path):
        logger.info(f"Creating build tar from /out -> {build_tar_path}")
        def _tar_out():
            with tarfile.open(build_tar_path, "w") as tf:
                tf.add("/out", arcname=".")
        await asyncio.to_thread(_tar_out)
    build_vfs = await TarFS.fsopen(build_tar_path)
    build_artifacts = BuildArtifacts(project_name, build_config, build_vfs)

    # Pre-populate builds so build_all() returns immediately
    state = await proj.edit_state()
    proj.builds[state][build_config] = build_artifacts

    # Pre-populate coverage build if available (from build phase)
    await _populate_optional_build(
        proj, state, info.coverage_build_config,
        "/out-coverage", "coverage",
    )

    # Pre-populate debug build if available (from build phase)
    await _populate_optional_build(
        proj, state, info.debug_build_config,
        "/out-debug", "debug",
    )

    # Pre-populate bear tar if compile_commands.json exists in /src
    # (generated by bear during the build phase). This enables infer
    # static analysis without needing to rebuild with bear at runtime.
    if os.path.exists("/src/compile_commands.json"):
        bear_tar_path = await proj.get_bear_tar()
        if not os.path.exists(bear_tar_path):
            logger.info(f"Creating bear tar from /src (has compile_commands.json) -> {bear_tar_path}")
            def _tar_bear():
                with tarfile.open(bear_tar_path, "w") as tf:
                    tf.add("/src", arcname=".")
            await asyncio.to_thread(_tar_bear)
            logger.info("Pre-populated bear tar for infer static analysis")
    else:
        logger.info("No compile_commands.json in /src — infer static analysis unavailable")

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

        if task_detail.type == TaskType.TaskTypeDelta:
            diff_text = _read_diff_from_sources(task_detail)
            if diff_text:
                # Base project: same source, NO build artifacts.
                # Without builds, DeltaTask.test_pov_contents() gets Err from
                # base.run_pov() → all POVs are accepted (can't verify regressions
                # without separately compiled base binaries).
                base_proj = Project(
                    project_dir=proj.project_dir,
                    data_dir=proj.data_dir,
                    vfs=proj.vfs.fork(),
                    info=proj.info,
                    ossfuzz_hash=proj.ossfuzz_hash,
                    harnesses=proj.harnesses,
                    workdir=proj._working_dir,
                )

                # Rewrite diff paths to match project VFS layout
                match await rewrite_diff(proj, diff_text):
                    case Ok(rewritten):
                        diff_text = rewritten
                    case Err(e):
                        logger.warning(f"Failed to rewrite diff: {e}; using raw diff")

                logger.info(f"Created DeltaTask with {len(diff_text)} byte diff")
                return Ok(project.DeltaTask(
                    task_detail.task_id,
                    task_detail.deadline,
                    proj,
                    CoverageAnalyzer(proj),
                    Debugger(proj),
                    task_detail.metadata,
                    base_proj,
                    diff_text,
                ))
            else:
                logger.warning("Delta task requested but no diff found; running as full task")

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


def _read_diff_from_sources(task_detail: TaskDetail) -> str | None:
    """Extract diff text from oss-crs task sources."""
    for source in task_detail.source:
        if source.type == SourceType.SourceTypeDiff:
            diff_path = source.url.removeprefix("file://")
            if os.path.exists(diff_path):
                with open(diff_path) as f:
                    return f.read()
            logger.warning(f"Diff source URL {source.url} points to non-existent path {diff_path}")
            return None
    return None
