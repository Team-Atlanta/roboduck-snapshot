"""Tests for OSSCRSSubmitter — filesystem-based artifact submission."""
import os
import uuid

import pytest

from crs.app.models import (
    POVSubmissionResponse,
    PatchSubmissionResponse,
    BundleSubmissionResponseVerbose,
    SarifAssessmentResponse,
)
from crs.app.oss_crs_submitter import OSSCRSSubmitter
from crs.app.products_db import ProductsDB
from crs.common.types import POVRunData, PatchRes, VulnReport


@pytest.fixture
def artifact_dirs(tmp_path):
    """Create temp artifact directories and set env vars."""
    pov_dir = tmp_path / "povs"
    seed_dir = tmp_path / "seeds"
    patch_dir = tmp_path / "patches"
    pov_dir.mkdir()
    seed_dir.mkdir()
    patch_dir.mkdir()

    old_pov = os.environ.get("OSS_CRS_POV_DIR")
    old_seed = os.environ.get("OSS_CRS_SEED_DIR")
    old_patch = os.environ.get("OSS_CRS_PATCH_DIR")

    os.environ["OSS_CRS_POV_DIR"] = str(pov_dir)
    os.environ["OSS_CRS_SEED_DIR"] = str(seed_dir)
    os.environ["OSS_CRS_PATCH_DIR"] = str(patch_dir)

    yield {"pov": pov_dir, "seed": seed_dir, "patch": patch_dir}

    # Restore
    for key, old in [
        ("OSS_CRS_POV_DIR", old_pov),
        ("OSS_CRS_SEED_DIR", old_seed),
        ("OSS_CRS_PATCH_DIR", old_patch),
    ]:
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old


@pytest.fixture
def db(tmp_path):
    return ProductsDB(db_path=tmp_path / "test_products.sqlite3")


@pytest.fixture
def submitter(artifact_dirs, db, monkeypatch):
    """Create OSSCRSSubmitter pointing to temp dirs."""
    # Override module-level constants that were captured at import time
    import crs.app.oss_crs_submitter as mod
    monkeypatch.setattr(mod, "POV_DIR", str(artifact_dirs["pov"]))
    monkeypatch.setattr(mod, "SEED_DIR", str(artifact_dirs["seed"]))
    monkeypatch.setattr(mod, "PATCH_DIR", str(artifact_dirs["patch"]))
    return OSSCRSSubmitter(db=db)


def _make_pov(task_id: uuid.UUID) -> POVRunData:
    return POVRunData(
        task_uuid=task_id,
        project_name="test-proj",
        harness="fuzz_target",
        sanitizer="address",
        engine="libfuzzer",
        python=None,
        input=b"CRASH_INPUT_DATA",
        output="ASAN error detected",
        dedup="abc123def456" * 4,
        stack="frame0\nframe1\nframe2",
    )


def _make_patch(task_id: uuid.UUID) -> PatchRes:
    return PatchRes(
        task_uuid=task_id,
        project_name="test-proj",
        diff="--- a/file.c\n+++ b/file.c\n@@ -1 +1 @@\n-bad\n+good\n",
        vuln_id=1,
        artifacts=[],
    )


class TestOSSCRSSubmitterPing:
    async def test_ping_returns_true(self, submitter):
        assert await submitter.ping() is True


class TestOSSCRSSubmitterPOV:
    async def test_submit_pov_writes_file(self, submitter, artifact_dirs):
        task_id = uuid.uuid4()
        pov = _make_pov(task_id)

        response = await submitter.submit_pov(task_id, 1, pov)

        assert isinstance(response, POVSubmissionResponse)
        assert response.status == "accepted"

        # Verify file was written
        pov_files = list(artifact_dirs["pov"].iterdir())
        assert len(pov_files) == 1
        assert pov_files[0].suffix == ".bin"
        assert pov_files[0].read_bytes() == b"CRASH_INPUT_DATA"

    async def test_submit_multiple_povs(self, submitter, artifact_dirs):
        task_id = uuid.uuid4()
        pov = _make_pov(task_id)

        r1 = await submitter.submit_pov(task_id, 1, pov)
        r2 = await submitter.submit_pov(task_id, 2, pov)

        assert r1.pov_id != r2.pov_id
        pov_files = list(artifact_dirs["pov"].iterdir())
        assert len(pov_files) == 2

    async def test_poll_pov_marks_passed(self, submitter):
        task_id = uuid.uuid4()
        pov = _make_pov(task_id)
        response = await submitter.submit_pov(task_id, 1, pov)
        assert response.status == "accepted"

        polled = await submitter.poll_pov(task_id, 1, response)
        assert polled.status == "passed"


class TestOSSCRSSubmitterPatch:
    async def test_submit_patch_writes_file(self, submitter, artifact_dirs):
        task_id = uuid.uuid4()
        patch = _make_patch(task_id)

        response = await submitter.submit_patch(task_id, 1, patch)

        assert isinstance(response, PatchSubmissionResponse)
        assert response.status == "accepted"

        patch_files = list(artifact_dirs["patch"].iterdir())
        assert len(patch_files) == 1
        assert patch_files[0].suffix == ".diff"
        content = patch_files[0].read_text()
        assert "+good" in content

    async def test_poll_patch_marks_passed(self, submitter):
        task_id = uuid.uuid4()
        patch = _make_patch(task_id)
        response = await submitter.submit_patch(task_id, 1, patch)

        polled = await submitter.poll_patch(task_id, 1, response)
        assert polled.status == "passed"


class TestOSSCRSSubmitterSarif:
    async def test_submit_sarif_assessment_returns_response(self, submitter):
        task_id = uuid.uuid4()
        sarif_id = uuid.uuid4()

        response = await submitter.submit_sarif_assessment(
            task_id, vuln_id=1, sarif_id=sarif_id, correct=True, reason="matches"
        )

        assert isinstance(response, SarifAssessmentResponse)
        assert response.status == "accepted"
        assert response.sarif_id == sarif_id

    async def test_submit_sarif_assessment_no_vuln_id(self, submitter):
        task_id = uuid.uuid4()
        sarif_id = uuid.uuid4()

        response = await submitter.submit_sarif_assessment(
            task_id, vuln_id=None, sarif_id=sarif_id, correct=False, reason="no match"
        )

        assert response.status == "accepted"


class TestOSSCRSSubmitterBundle:
    async def test_submit_bundle(self, submitter):
        task_id = uuid.uuid4()
        pov_id = uuid.uuid4()
        patch_id = uuid.uuid4()

        response = await submitter.submit_bundle(
            task_id, bundle_id=1, description="test bundle",
            pov_id=pov_id, patch_id=patch_id,
        )

        assert isinstance(response, BundleSubmissionResponseVerbose)
        assert response.status == "accepted"
        assert response.pov_id == pov_id
        assert response.patch_id == patch_id

    async def test_update_bundle(self, submitter):
        task_id = uuid.uuid4()
        bundle_sub_id = uuid.uuid4()

        response = await submitter.update_bundle(
            task_id, bundle_id=1, bundle_sub_id=bundle_sub_id,
            description="updated bundle",
        )

        assert response.status == "accepted"
        assert response.bundle_id == bundle_sub_id

    async def test_delete_bundle_no_error(self, submitter):
        task_id = uuid.uuid4()
        bundle_sub_id = uuid.uuid4()
        # Should not raise
        await submitter.delete_bundle(task_id, bundle_id=1, bundle_sub_id=bundle_sub_id)


class TestOSSCRSSubmitterRejectedSarif:
    async def test_submit_rejected_sarif_no_error(self, submitter):
        report = VulnReport(
            task_uuid=uuid.uuid4(),
            project_name="test-proj",
            function="vuln_func",
            file="src/vuln.c",
            description="buffer overflow",
        )
        # Should not raise
        await submitter.submit_rejected_sarif_report(report, "not applicable")
