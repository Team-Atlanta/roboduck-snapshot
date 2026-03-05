"""
OSSCRSSubmitter — artifact submission for oss-crs mode.

Writes POVs and seeds to directories monitored by `libCRS register-submit-dir`.
Replaces the Competition API (CAPI) submission flow used in AIxCC.
"""
import os
import uuid

from typing import Optional
from uuid import UUID

from crs.app.models import (
    POVSubmissionResponse,
    PatchSubmissionResponse,
    BundleSubmissionResponseVerbose,
    SarifAssessmentResponse,
)
from crs.app.products_db import ProductsDB
from crs.common.types import POVRunData, VulnReport, PatchRes

from crs_rust import logger

POV_DIR = os.environ.get("OSS_CRS_POV_DIR", "/artifacts/povs")
SEED_DIR = os.environ.get("OSS_CRS_SEED_DIR", "/artifacts/seeds")
PATCH_DIR = os.environ.get("OSS_CRS_PATCH_DIR", "/artifacts/patches")


class OSSCRSSubmitter:
    """
    Drop-in replacement for Submitter that writes artifacts to the filesystem
    instead of submitting them to the Competition API.

    libCRS register-submit-dir watches these directories and handles actual
    submission to the oss-crs infrastructure.
    """

    def __init__(self, db: Optional[ProductsDB] = None):
        self.db = db or ProductsDB()
        os.makedirs(POV_DIR, exist_ok=True)
        os.makedirs(SEED_DIR, exist_ok=True)
        os.makedirs(PATCH_DIR, exist_ok=True)

    async def ping(self) -> bool:
        return True

    async def submit_pov(
        self, task_id: UUID, pov_id: int, pov: POVRunData
    ) -> POVSubmissionResponse:
        pov_uuid = uuid.uuid4()
        pov_path = os.path.join(POV_DIR, f"{pov_uuid}.bin")

        with open(pov_path, "wb") as f:
            f.write(pov.input)

        logger.info(
            f"[oss-crs] POV submitted: {pov_path} "
            f"(task={task_id}, pov_id={pov_id}, harness={pov.harness}, "
            f"dedup={pov.dedup[:32]}...)"
        )

        response = POVSubmissionResponse(
            pov_id=pov_uuid,
            status="accepted",
        )
        await self.db.put_submission(task_id, response, "povs", pov_id)
        return response

    async def submit_patch(
        self, task_id: UUID, patch_id: int, patch: PatchRes
    ) -> PatchSubmissionResponse:
        patch_uuid = uuid.uuid4()
        patch_path = os.path.join(PATCH_DIR, f"{patch_uuid}.diff")

        with open(patch_path, "w") as f:
            f.write(patch.diff)

        logger.info(
            f"[oss-crs] Patch submitted: {patch_path} "
            f"(task={task_id}, patch_id={patch_id})"
        )

        response = PatchSubmissionResponse(
            patch_id=patch_uuid,
            status="accepted",
        )
        await self.db.put_submission(task_id, response, "patches", patch_id)
        return response

    async def submit_sarif_assessment(
        self,
        task_id: UUID,
        vuln_id: Optional[int],
        sarif_id: UUID,
        correct: bool,
        reason: str,
    ) -> SarifAssessmentResponse:
        logger.info(
            f"[oss-crs] SARIF assessment (no-op): task={task_id} "
            f"vuln={vuln_id} correct={correct}"
        )
        response = SarifAssessmentResponse(
            status="accepted",
            sarif_id=sarif_id,
        )
        await self.db.put_submission(
            task_id, response, "vulns" if vuln_id else None, vuln_id
        )
        return response

    async def submit_bundle(
        self,
        task_id: UUID,
        bundle_id: int,
        description: str,
        patch_id: Optional[UUID] = None,
        pov_id: Optional[UUID] = None,
        broadcast_sarif_id: Optional[UUID] = None,
        submitted_sarif_id: Optional[UUID] = None,
    ) -> BundleSubmissionResponseVerbose:
        bundle_uuid = uuid.uuid4()
        logger.info(
            f"[oss-crs] Bundle submitted (no-op): bundle={bundle_uuid} "
            f"task={task_id} pov={pov_id} patch={patch_id} desc={description[:80]}"
        )
        response = BundleSubmissionResponseVerbose(
            bundle_id=bundle_uuid,
            status="accepted",
            description=description,
            patch_id=patch_id,
            pov_id=pov_id,
        )
        await self.db.put_submission(task_id, response, "bundles", bundle_id)
        return response

    async def update_bundle(
        self,
        task_id: UUID,
        bundle_id: int,
        bundle_sub_id: UUID,
        description: str,
        patch_id: Optional[UUID] = None,
        pov_id: Optional[UUID] = None,
        broadcast_sarif_id: Optional[UUID] = None,
        submitted_sarif_id: Optional[UUID] = None,
    ) -> BundleSubmissionResponseVerbose:
        logger.info(f"[oss-crs] Bundle update (no-op): bundle={bundle_sub_id}")
        response = BundleSubmissionResponseVerbose(
            bundle_id=bundle_sub_id,
            status="accepted",
            description=description,
            patch_id=patch_id,
            pov_id=pov_id,
        )
        await self.db.put_submission(task_id, response, "bundles", bundle_id)
        return response

    async def delete_bundle(
        self,
        task_id: UUID,
        bundle_id: int,
        bundle_sub_id: UUID,
    ):
        logger.info(f"[oss-crs] Bundle delete (no-op): bundle={bundle_sub_id}")

    async def poll_pov(
        self, task_id: UUID, pov_id: int, response: POVSubmissionResponse
    ) -> POVSubmissionResponse:
        # In oss-crs mode, POVs don't go through a competition server
        # so we just mark them as "passed" immediately
        response.status = "passed"
        await self.db.put_submission(task_id, response, "povs", pov_id)
        return response

    async def poll_patch(
        self, task_id: UUID, patch_id: int, response: PatchSubmissionResponse
    ) -> PatchSubmissionResponse:
        response.status = "passed"
        await self.db.put_submission(task_id, response, "patches", patch_id)
        return response

    async def submit_rejected_sarif_report(self, report: VulnReport, reason: str):
        logger.info(f"[oss-crs] Rejected SARIF (no-op): {reason[:80]}")
