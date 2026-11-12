"""Cleaning job lifecycle, query-only upload, recipe approval, and artifact download routes."""

from __future__ import annotations

import json
import logging
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.convert_store import save_convert_job_meta
from app.dependencies import (
    copy_stream_to_path,
    get_convert_jobs,
    get_max_upload_bytes,
    get_orchestrator_dep,
    get_retention_manager,
    get_storage,
)
from app.models import JobDetailResponse, JobMetadata
from core.security import resolve_within_base, sanitize_filename, validate_safe_id
from core.tenant import get_current_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Cleaning Jobs"])


@router.post("/query/upload")
async def upload_dataset_for_query_only(
    file: UploadFile = File(...),
    options: Optional[str] = Form(None),
    user_id: str = Depends(get_current_tenant_id),
):
    """Stage an uploaded dataset directly into a sandboxed queryable Parquet store for instant AI and SQL exploration without cleaning or format conversion."""
    from formats import AdapterRegistry, convert_to_parquet, detect_format

    convert_jobs = get_convert_jobs()
    query_id = f"query_{uuid.uuid4().hex[:8]}"
    work_dir = Path("data/query_jobs").resolve() / user_id / query_id
    input_file_path = work_dir / file.filename

    parsed_options = None
    if options:
        try:
            parsed_options = json.loads(options)
        except Exception:
            pass

    suffix = Path(file.filename).suffix.lower()
    target_parquet = work_dir / f"{Path(file.filename).stem}.parquet"

    def _stage_query_file():
        work_dir.mkdir(parents=True, exist_ok=True)
        size_bytes = copy_stream_to_path(file.file, input_file_path)
        detected_fmt, _ = detect_format(input_file_path)
        if detected_fmt == "unknown" and suffix in (".csv", ".tsv", ".parquet", ".json", ".ndjson", ".xlsx", ".db", ".sqlite"):
            adapter = AdapterRegistry.find_by_extension(suffix)
            if adapter:
                detected_fmt = adapter.format_id
        conversion_report = convert_to_parquet(
            path=input_file_path,
            out_path=target_parquet,
            format_id=detected_fmt if detected_fmt != "unknown" else None,
            options=parsed_options,
        )
        return detected_fmt, conversion_report, size_bytes

    try:
        detected_fmt, conversion_report, size_bytes = await run_in_threadpool(_stage_query_file)

        # Register in convert_jobs and persist meta.json so handoff to clean survives restarts
        convert_jobs[query_id] = {
            "id": query_id,
            "user_id": user_id,
            "filename": file.filename,
            "work_dir": work_dir,
            "intermediate_parquet": target_parquet,
        }
        save_convert_job_meta(convert_jobs[query_id])

        return {
            "query_id": query_id,
            "filename": file.filename,
            "row_count": conversion_report.rows,
            "columns": conversion_report.columns,
            "file_size_bytes": size_bytes,
            "detected_format": detected_fmt,
        }
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        logger.exception("Failed to stage dataset for query: %s", e)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/jobs/upload", response_model=JobMetadata)
async def upload_dataset(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    privacy_mode: bool = Form(False),
    format: Optional[str] = Form(None),
    options: Optional[str] = Form(None),
    user_id: str = Depends(get_current_tenant_id),
):
    """Upload a dataset (CSV, TSV, Parquet, JSON, NDJSON, Excel, SQLite) and schedule pipeline."""
    from formats import AdapterRegistry

    orchestrator = get_orchestrator_dep()
    storage = get_storage()
    ext = Path(file.filename).suffix.lower()

    supported_adapter = (
        (format and AdapterRegistry.get(format))
        or AdapterRegistry.find_by_extension(ext)
    )
    if not supported_adapter and ext not in (".csv", ".tsv", ".parquet", ".json", ".ndjson", ".jsonl", ".xlsx", ".db", ".sqlite"):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Supported formats: CSV, TSV, Parquet, JSON, NDJSON, Excel (.xlsx), SQLite (.db).",
        )

    parsed_options = None
    if options:
        try:
            parsed_options = json.loads(options)
        except Exception:
            pass

    job = orchestrator.create_job(
        filename=file.filename,
        file_size=0,
        privacy_mode=privacy_mode,
        format_id=format,
        adapter_options=parsed_options,
        user_id=user_id,
    )

    dest_path = storage.raw_file_path(job.id, file.filename, user_id=user_id)
    job.file_size_bytes = await run_in_threadpool(copy_stream_to_path, file.file, dest_path)
    orchestrator._save_job_state(job.id)

    background_tasks.add_task(orchestrator.run_profile_and_ai_draft, job.id)

    return job


@router.get("/jobs/{job_id}/status", response_model=JobMetadata)
def get_job_status_lightweight(job_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Lightweight polling endpoint returning only JobMetadata without heavy profile/report payloads."""
    orchestrator = get_orchestrator_dep()
    retention_manager = get_retention_manager()
    meta = orchestrator.get_job_status_snapshot(job_id, user_id=user_id)
    if not meta:
        ttl_h = int(retention_manager.config.ttl_hours)
        raise HTTPException(
            status_code=404,
            detail=f"Job '{job_id}' not found or has expired ({ttl_h}h retention policy). Please re-upload your dataset.",
        )
    return meta


@router.get("/jobs/{job_id}", response_model=JobDetailResponse)
def get_job_status(job_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Fetch current progress, profile, AI report, or verification results."""
    orchestrator = get_orchestrator_dep()
    retention_manager = get_retention_manager()
    detail = orchestrator.get_job_detail(job_id, user_id=user_id)
    if not detail:
        ttl_h = int(retention_manager.config.ttl_hours)
        raise HTTPException(
            status_code=404,
            detail=f"Job '{job_id}' not found or has expired ({ttl_h}h retention policy). Please re-upload your dataset.",
        )
    return detail


@router.get("/jobs/{job_id}/export-options")
def get_job_export_options(job_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Get all export formats available for this job."""
    from formats import list_export_options

    orchestrator = get_orchestrator_dep()
    job = orchestrator.get_job(job_id, user_id=user_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
    return list_export_options(job.detected_format or "csv", job.row_count)


@router.put("/jobs/{job_id}/recipe")
def update_recipe(
    job_id: str,
    recipe: Dict[str, Any],
    user_id: str = Depends(get_current_tenant_id),
):
    """Update recipe choices or confirmation responses."""
    orchestrator = get_orchestrator_dep()
    updated = orchestrator.update_recipe(job_id, recipe, user_id=user_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
    return {"status": "updated", "job_id": job_id}


@router.post("/jobs/{job_id}/clean")
def approve_and_clean(
    job_id: str,
    background_tasks: BackgroundTasks,
    recipe: Optional[Dict[str, Any]] = None,
    user_id: str = Depends(get_current_tenant_id),
):
    """Approve recipe and start DuckDB CTE cleaning and verification."""
    orchestrator = get_orchestrator_dep()
    job = orchestrator.get_job(job_id, user_id=user_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired.")

    background_tasks.add_task(orchestrator.execute_cleaning_and_verification, job_id, recipe, user_id=user_id)
    return {"status": "started", "job_id": job_id}


@router.get("/jobs/{job_id}/download/{format}")
def download_artifact(
    job_id: str,
    format: str,
    user_id: str = Depends(get_current_tenant_id),
):
    """Download cleaned dataset in any supported format (CSV, Parquet, TSV, NDJSON, JSON, XLSX) or verification report."""
    from formats import ExporterRegistry

    orchestrator = get_orchestrator_dep()
    storage = get_storage()
    job = orchestrator.get_job(job_id, user_id=user_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired.")

    format_lower = format.lower()

    if format_lower == "report":
        file_path = storage.verification_json_path(job_id, user_id=job.user_id)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Verification report not yet generated.")
        return FileResponse(path=file_path, media_type="application/json", filename=f"verification_{job.id}.json")

    if not ExporterRegistry.get(format_lower):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid format '{format}'. Supported: csv, tsv, parquet, ndjson, json, xlsx, report.",
        )

    try:
        created_paths = orchestrator.export_dataset(job_id, format_lower, user_id=job.user_id)
        if not created_paths:
            raise HTTPException(status_code=500, detail="Export failed to generate files.")

        if len(created_paths) == 1:
            file_path = created_paths[0]
            exporter = ExporterRegistry.get(format_lower)
            media_type = exporter.mime_type if exporter else "application/octet-stream"
            base_name = Path(job.filename).stem
            filename = f"cleaned_{base_name}{exporter.extension if exporter else ''}"
            return FileResponse(path=file_path, media_type=media_type, filename=filename)

        zip_path = storage.job_dir(job_id, user_id=job.user_id) / f"cleaned_{Path(job.filename).stem}_{format_lower}_parts.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in created_paths:
                zf.write(p, arcname=p.name)
        return FileResponse(
            path=zip_path,
            media_type="application/zip",
            filename=zip_path.name,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Export failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Export error: {e}")

