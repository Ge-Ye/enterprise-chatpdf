"""Customs extraction API endpoints."""

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.core.config import settings
from app.schemas.customs_schema import CustomsExtractionResponse, ExtractCustomsRequest
from app.services import file_registry
from app.services.extractor_service import CustomsExtractionError, extract_customs_dual_path
from app.services.validator_service import build_compliance_rule_tips, send_feishu_webhook_alert, validate_customs_declaration

logger = logging.getLogger(__name__)
router = APIRouter(tags=["customs"])


def _resolve_upload(file_id: Optional[str], file_name: Optional[str]) -> Optional[str]:
    if not file_id and not file_name:
        return None
    try:
        records = file_registry.list_records()
    except (OSError, ValueError, file_registry.FileRegistryError):
        logger.exception("Could not read upload metadata for customs extraction")
        raise HTTPException(status_code=500, detail="无法读取已上传文件列表")

    matched = next((entry for entry in records if file_id and entry.get("id") == file_id), None)
    if matched is None and file_name:
        matched = next((entry for entry in records if file_name in (entry.get("name"), entry.get("alias"))), None)
    if matched is None:
        raise HTTPException(status_code=404, detail="未找到对应的已上传文件")

    candidate = Path(str(matched.get("path", ""))).resolve()
    upload_root = settings.upload_path.resolve()
    try:
        candidate.relative_to(upload_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="上传文件路径无效") from exc
    if candidate.suffix.lower() != ".pdf" or not candidate.is_file():
        raise HTTPException(status_code=400, detail="所选文件不是可读取的 PDF")
    return str(candidate)


def _extract(request: ExtractCustomsRequest) -> CustomsExtractionResponse:
    file_path = _resolve_upload(request.file_id, request.file_name)
    try:
        extraction = extract_customs_dual_path(
            file_path, request.mode, allow_fallback=request.allow_fallback
        )
    except CustomsExtractionError as exc:
        logger.warning(
            "Customs extraction rejected (status=%s, reasons=%s): %s",
            exc.status_code, exc.reasons, exc.message,
        )
        raise HTTPException(
            status_code=exc.status_code,
            detail={"message": exc.message, "degraded_reasons": exc.reasons},
        )

    issues = validate_customs_declaration(extraction.data, extraction.vision_data)

    # Only model-extracted data may page the customs team; demo/fallback samples are dry-run.
    alert_sent = False
    alert_time = None
    if extraction.extraction_source == "real" and issues:
        alert_sent, alert_time = send_feishu_webhook_alert(extraction.data, issues)
    elif issues:
        logger.info(
            "Skipping Feishu alert for %s result: demo/fallback data is dry-run only",
            extraction.extraction_source,
        )

    if extraction.extraction_source != "real":
        status = "DEMO"
    elif issues:
        status = "NEEDS_REVIEW"
    elif extraction.degraded_reasons or extraction.vision_status != "ok":
        # A real result missing a path cannot claim full dual-model agreement.
        status = "DEGRADED"
    else:
        status = "PASSED"

    return CustomsExtractionResponse(
        status=status,
        extraction_source=extraction.extraction_source,
        vision_status=extraction.vision_status,
        degraded_reasons=extraction.degraded_reasons,
        feishu_alert_sent=alert_sent,
        feishu_alert_time=alert_time,
        compliance_tip=build_compliance_rule_tips(extraction.data) or None,
        data=extraction.data,
        issues=issues,
    )


@router.post("/extract_customs", response_model=CustomsExtractionResponse)
@router.post("/api/extract_customs", response_model=CustomsExtractionResponse, include_in_schema=False)
def extract_customs(request: ExtractCustomsRequest = ExtractCustomsRequest()) -> CustomsExtractionResponse:
    return _extract(request)
