"""Format detection, preview, and convert-only routes."""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.convert_store import get_convert_job, save_convert_job_meta
from app.dependencies import copy_stream_to_path, get_convert_jobs, get_orchestrator_dep, get_storage
from core.tenant import get_current_tenant_id

if TYPE_CHECKING:
    from formats.convert import ConvertResult

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Formats & Convert"])


@router.post("/formats/detect")
async def detect_uploaded_format(file: UploadFile = File(...)):
    """Sniff and detect the format of an uploaded file without saving permanently."""
    suffix = Path(file.filename).suffix
    chunk = await file.read(2 * 1024 * 1024)

    def _inspect_sample():
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(chunk)
            tmp_path = Path(tmp.name)
        try:
            from formats import AdapterRegistry, detect_format

            format_id, confidence = detect_format(tmp_path)
            adapter = AdapterRegistry.get(format_id) or AdapterRegistry.find_by_extension(suffix)

            extra_info: Dict[str, Any] = {}
            if adapter:
                if hasattr(adapter, "list_sheets"):
                    extra_info["sheets"] = adapter.list_sheets(tmp_path)
                if hasattr(adapter, "list_tables"):
                    extra_info["tables"] = adapter.list_tables(tmp_path)

            return {
                "format": format_id,
                "confidence": confidence,
                "filename": file.filename,
                **extra_info,
            }
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    return await run_in_threadpool(_inspect_sample)


@router.post("/convert")
async def preview_and_convert(
    file: UploadFile = File(...),
    format: Optional[str] = Form(None),
    options: Optional[str] = Form(None),
):
    """Preview an input dataset returning preview rows and ConversionReport."""
    suffix = Path(file.filename).suffix

    def _run_preview():
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            shutil.copyfileobj(file.file, tmp, length=1024 * 1024)
            tmp_path = Path(tmp.name)
        try:
            from formats import AdapterRegistry, detect_format

            parsed_options = json.loads(options) if options else {}
            fmt = format
            if not fmt or fmt == "auto":
                fmt, _ = detect_format(tmp_path)

            adapter = AdapterRegistry.get(fmt) or AdapterRegistry.find_by_extension(suffix)
            if not adapter:
                raise HTTPException(status_code=400, detail=f"Unsupported format '{fmt}'.")

            preview_rows, report = adapter.preview(tmp_path, options=parsed_options, limit=100)
            return {
                "preview": preview_rows,
                "report": report.to_dict(),
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    return await run_in_threadpool(_run_preview)


@router.post("/convert/execute")
async def execute_convert_only(
    file: UploadFile = File(...),
    target_format: str = Form(...),
    source_format: Optional[str] = Form(None),
    source_options: Optional[str] = Form(None),
    target_options: Optional[str] = Form(None),
    user_id: str = Depends(get_current_tenant_id),
):
    """Execute Convert-Only mode: direct conversion between formats without profiling or cleaning."""
    from formats import convert_file

    convert_jobs = get_convert_jobs()
    convert_id = f"conv_{uuid.uuid4().hex[:8]}"
    work_dir = Path("data/convert_jobs").resolve() / user_id / convert_id
    input_file_path = work_dir / file.filename
    parsed_src_opts = json.loads(source_options) if source_options else None
    parsed_tgt_opts = json.loads(target_options) if target_options else None

    def _run_convert():
        work_dir.mkdir(parents=True, exist_ok=True)
        copy_stream_to_path(file.file, input_file_path)
        return convert_file(
            input_path=input_file_path,
            target_format=target_format,
            work_dir=work_dir,
            source_format=source_format if source_format and source_format != "auto" else None,
            source_options=parsed_src_opts,
            target_options=parsed_tgt_opts,
            keep_intermediate=True,
        )

    try:
        result = await run_in_threadpool(_run_convert)
        convert_jobs[convert_id] = {
            "id": convert_id,
            "user_id": user_id,
            "result": result,
            "filename": file.filename,
            "work_dir": work_dir,
            "intermediate_parquet": result.intermediate_parquet,
        }
        save_convert_job_meta(convert_jobs[convert_id])

        return {
            "convert_id": convert_id,
            "result": result.to_dict(),
        }
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        logger.exception("Convert-only execution failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/convert/download/{convert_id}")
def download_converted_file(convert_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Download converted output file(s). Zips if multiple parts were generated."""
    convert_jobs = get_convert_jobs()
    job = get_convert_job(convert_jobs, convert_id, user_id)
    if not job:
        raise HTTPException(status_code=404, detail="Convert job not found or expired.")

    result: "ConvertResult" = job["result"]
    output_files = result.output_files

    if not output_files:
        raise HTTPException(status_code=404, detail="No output files available.")

    from formats import ExporterRegistry

    if len(output_files) == 1:
        fpath = output_files[0]
        exporter = ExporterRegistry.get(result.target_format)
        media_type = exporter.mime_type if exporter else "application/octet-stream"
        return FileResponse(path=fpath, media_type=media_type, filename=fpath.name)

    zip_path = job["work_dir"] / f"converted_{Path(job['filename']).stem}_{result.target_format}_parts.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in output_files:
            zf.write(p, arcname=p.name)
    return FileResponse(path=zip_path, media_type="application/zip", filename=zip_path.name)


@router.post("/convert/{convert_id}/handoff-to-clean")
def handoff_to_cleaning(
    convert_id: str,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_current_tenant_id),
):
    """Handoff converted intermediate Parquet directly to the cleaning pipeline without re-converting."""
    convert_jobs = get_convert_jobs()
    orchestrator = get_orchestrator_dep()
    storage = get_storage()

    job = get_convert_job(convert_jobs, convert_id, user_id)
    if not job:
        raise HTTPException(status_code=404, detail="Convert job not found or expired.")

    intermediate_pq = job.get("intermediate_parquet")
    if not intermediate_pq or not Path(intermediate_pq).exists():
        raise HTTPException(status_code=400, detail="Intermediate Parquet not available for handoff.")

    clean_job = orchestrator.create_job(
        filename=job["filename"],
        file_size=Path(intermediate_pq).stat().st_size,
        format_id="parquet",
        user_id=user_id,
    )

    target_staged_pq = storage.staged_parquet_path(clean_job.id, user_id=user_id)
    shutil.copy2(intermediate_pq, target_staged_pq)

    background_tasks.add_task(orchestrator.run_profile_and_ai_draft, clean_job.id)

    return clean_job

