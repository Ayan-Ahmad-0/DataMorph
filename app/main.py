"""FastAPI application providing REST endpoints for the AI Data Cleaner."""

from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional
from dotenv import load_dotenv

load_dotenv()
import duckdb
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from app.convert_store import get_convert_job, save_convert_job_meta
from app.models import JobDetailResponse, JobMetadata
from app.orchestrator import JobOrchestrator, get_orchestrator
from app.storage import JobStorage
from connectors import connectors_router
from core.retention import StorageRetentionManager, retention_daemon_loop
from core.tenant import get_current_tenant_id

logger = logging.getLogger(__name__)

storage = JobStorage()
orchestrator = get_orchestrator(storage=storage)
retention_manager = StorageRetentionManager()
convert_jobs: Dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI application lifespan: manages background retention scavenger loop."""
    daemon_task = None
    if retention_manager.config.enabled:
        daemon_task = asyncio.create_task(
            retention_daemon_loop(
                manager=retention_manager,
                orchestrator=orchestrator,
                convert_jobs=convert_jobs,
            )
        )
        logger.info(
            "Initialized storage retention background daemon (TTL: %.1fh, interval: %dm).",
            retention_manager.config.ttl_hours,
            retention_manager.config.cleanup_interval_minutes,
        )
    yield
    if daemon_task and not daemon_task.done():
        logger.info("Stopping storage retention background daemon.")
        daemon_task.cancel()
        try:
            await daemon_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="One-Click AI Data Cleaner API",
    description="DuckDB-powered profile-guided AI data cleaning pipeline",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(connectors_router)

@app.middleware("http")
async def add_no_cache_for_html(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in ("/", "/index.html") or request.url.path.endswith(".html"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# Enable CORS for frontend development
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$|^https://([a-zA-Z0-9-]+\.)*huggingface\.co$|^https://([a-zA-Z0-9-]+\.)*hf\.space$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health_check():
    return {"status": "ok", "engine": "DuckDB", "service": "data-cleaner-api"}


@app.post("/api/maintenance/cleanup")
def trigger_storage_cleanup(max_age_hours: Optional[float] = None):
    """Trigger an on-demand retention sweep across memory and dynamic storage directories."""
    result = retention_manager.purge_expired_data(
        orchestrator=orchestrator,
        convert_jobs=convert_jobs,
        max_age_hours=max_age_hours,
    )
    return result.to_dict()


@app.get("/api/maintenance/storage")
def get_storage_inventory_and_status():
    """Retrieve disk capacity, active category item counts, and retention policy settings."""
    inventory = retention_manager.get_storage_inventory()
    inventory["retention_policy"] = {
        "ttl_hours": retention_manager.config.ttl_hours,
        "cleanup_interval_minutes": retention_manager.config.cleanup_interval_minutes,
        "enabled": retention_manager.config.enabled,
        "max_disk_usage_percent": retention_manager.config.max_disk_usage_percent,
    }
    return inventory


@app.post("/api/formats/detect")
async def detect_uploaded_format(file: UploadFile = File(...)):
    """Sniff and detect the format of an uploaded file without saving permanently."""
    import tempfile
    suffix = Path(file.filename).suffix
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        # Sniffing only needs the initial sample (magic bytes, headers, delimiters)
        chunk = await file.read(2 * 1024 * 1024)
        tmp.write(chunk)
        tmp_path = Path(tmp.name)

    try:
        from formats import detect_format, AdapterRegistry
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


@app.post("/api/convert")
async def preview_and_convert(
    file: UploadFile = File(...),
    format: Optional[str] = Form(None),
    options: Optional[str] = Form(None),
):
    """Preview an input dataset returning preview rows and ConversionReport."""
    import tempfile
    suffix = Path(file.filename).suffix
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
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
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


@app.post("/api/convert/execute")
async def execute_convert_only(
    file: UploadFile = File(...),
    target_format: str = Form(...),
    source_format: Optional[str] = Form(None),
    source_options: Optional[str] = Form(None),
    target_options: Optional[str] = Form(None),
    user_id: str = Depends(get_current_tenant_id),
):
    """Execute Convert-Only mode: direct conversion between formats without profiling or cleaning."""
    import uuid
    import tempfile
    from formats import convert_file

    convert_id = f"conv_{uuid.uuid4().hex[:8]}"
    work_dir = Path("data/convert_jobs").resolve() / user_id / convert_id
    work_dir.mkdir(parents=True, exist_ok=True)

    input_file_path = work_dir / file.filename
    with open(input_file_path, "wb") as buf:
        shutil.copyfileobj(file.file, buf)

    parsed_src_opts = json.loads(source_options) if source_options else None
    parsed_tgt_opts = json.loads(target_options) if target_options else None

    try:
        result = convert_file(
            input_path=input_file_path,
            target_format=target_format,
            work_dir=work_dir,
            source_format=source_format if source_format and source_format != "auto" else None,
            source_options=parsed_src_opts,
            target_options=parsed_tgt_opts,
            keep_intermediate=True,  # keep for potential handoff to cleaning
        )

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


@app.get("/api/convert/download/{convert_id}")
def download_converted_file(convert_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Download converted output file(s). Zips if multiple parts were generated."""
    job = get_convert_job(convert_jobs, convert_id, user_id)
    if not job:
        raise HTTPException(status_code=404, detail="Convert job not found or expired.")

    result: ConvertResult = job["result"]
    output_files = result.output_files

    if not output_files:
        raise HTTPException(status_code=404, detail="No output files available.")

    from formats import ExporterRegistry
    if len(output_files) == 1:
        fpath = output_files[0]
        exporter = ExporterRegistry.get(result.target_format)
        media_type = exporter.mime_type if exporter else "application/octet-stream"
        return FileResponse(path=fpath, media_type=media_type, filename=fpath.name)
    else:
        # Multi-file output (e.g. split Excel) -> return ZIP
        import zipfile
        zip_path = job["work_dir"] / f"converted_{Path(job['filename']).stem}_{result.target_format}_parts.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in output_files:
                zf.write(p, arcname=p.name)
        return FileResponse(path=zip_path, media_type="application/zip", filename=zip_path.name)


@app.post("/api/convert/{convert_id}/handoff-to-clean")
def handoff_to_cleaning(
    convert_id: str,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_current_tenant_id),
):
    """Handoff converted intermediate Parquet directly to the cleaning pipeline without re-converting."""
    job = get_convert_job(convert_jobs, convert_id, user_id)
    if not job:
        raise HTTPException(status_code=404, detail="Convert job not found or expired.")

    intermediate_pq = job.get("intermediate_parquet")
    if not intermediate_pq or not Path(intermediate_pq).exists():
        raise HTTPException(status_code=400, detail="Intermediate Parquet not available for handoff.")

    # Create cleaning job tied to tenant
    clean_job = orchestrator.create_job(
        filename=job["filename"],
        file_size=Path(intermediate_pq).stat().st_size,
        format_id="parquet",
        user_id=user_id,
    )

    # Copy intermediate Parquet into orchestrator staged_parquet_path under tenant
    target_staged_pq = storage.staged_parquet_path(clean_job.id, user_id=user_id)
    shutil.copy2(intermediate_pq, target_staged_pq)

    # Launch background profiling & AI drafting on staged parquet
    background_tasks.add_task(orchestrator.run_profile_and_ai_draft, clean_job.id)

    return clean_job


@app.post("/api/query/upload")
async def upload_dataset_for_query_only(
    file: UploadFile = File(...),
    options: Optional[str] = Form(None),
    user_id: str = Depends(get_current_tenant_id),
):
    """Stage an uploaded dataset directly into a sandboxed queryable Parquet store for instant AI and SQL exploration without cleaning or format conversion."""
    import uuid
    from formats import convert_to_parquet, detect_format, AdapterRegistry

    query_id = f"query_{uuid.uuid4().hex[:8]}"
    work_dir = Path("data/query_jobs").resolve() / user_id / query_id
    work_dir.mkdir(parents=True, exist_ok=True)

    input_file_path = work_dir / file.filename
    with open(input_file_path, "wb") as buf:
        shutil.copyfileobj(file.file, buf, length=1024 * 1024)

    parsed_options = None
    if options:
        try:
            parsed_options = json.loads(options)
        except Exception:
            pass

    suffix = Path(file.filename).suffix.lower()

    try:
        detected_fmt, conf = detect_format(input_file_path)
        if detected_fmt == "unknown" and suffix in (".csv", ".tsv", ".parquet", ".json", ".ndjson", ".xlsx", ".db", ".sqlite"):
            adapter = AdapterRegistry.find_by_extension(suffix)
            if adapter:
                detected_fmt = adapter.format_id

        target_parquet = work_dir / f"{Path(file.filename).stem}.parquet"
        conversion_report = convert_to_parquet(
            path=input_file_path,
            out_path=target_parquet,
            format_id=detected_fmt if detected_fmt != "unknown" else None,
            options=parsed_options,
        )

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
            "file_size_bytes": input_file_path.stat().st_size,
            "detected_format": detected_fmt,
        }
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        logger.exception("Failed to stage dataset for query: %s", e)
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/jobs/upload", response_model=JobMetadata)
async def upload_dataset(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    privacy_mode: bool = Form(False),
    format: Optional[str] = Form(None),
    options: Optional[str] = Form(None),
    user_id: str = Depends(get_current_tenant_id),
):
    """Upload a dataset (CSV, TSV, Parquet, JSON, NDJSON, Excel, SQLite) and schedule pipeline."""
    from formats import AdapterRegistry, detect_format
    ext = Path(file.filename).suffix.lower()
    
    # Check if supported
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

    # Create job bound to user_id
    job = orchestrator.create_job(
        filename=file.filename,
        file_size=0,
        privacy_mode=privacy_mode,
        format_id=format,
        adapter_options=parsed_options,
        user_id=user_id,
    )

    # Save to tenant storage
    dest_path = storage.raw_file_path(job.id, file.filename, user_id=user_id)
    with open(dest_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    job.file_size_bytes = dest_path.stat().st_size

    # Launch background conversion and AI drafting
    background_tasks.add_task(orchestrator.run_profile_and_ai_draft, job.id)

    return job


@app.get("/api/jobs/{job_id}", response_model=JobDetailResponse)
def get_job_status(job_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Fetch current progress, profile, AI report, or verification results."""
    detail = orchestrator.get_job_detail(job_id, user_id=user_id)
    if not detail:
        ttl_h = int(retention_manager.config.ttl_hours)
        raise HTTPException(
            status_code=404,
            detail=f"Job '{job_id}' not found or has expired ({ttl_h}h retention policy). Please re-upload your dataset.",
        )
    return detail
@app.get("/api/jobs/{job_id}/export-options")
def get_job_export_options(job_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Get all export formats available for this job."""
    job = orchestrator.get_job(job_id, user_id=user_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
    from formats import list_export_options
    return list_export_options(job.detected_format or "csv", job.row_count)


@app.put("/api/jobs/{job_id}/recipe")
def update_recipe(
    job_id: str,
    recipe: Dict[str, Any],
    user_id: str = Depends(get_current_tenant_id),
):
    """Update recipe choices or confirmation responses."""
    updated = orchestrator.update_recipe(job_id, recipe, user_id=user_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
    return {"status": "updated", "job_id": job_id}


@app.post("/api/jobs/{job_id}/clean")
def approve_and_clean(
    job_id: str,
    background_tasks: BackgroundTasks,
    recipe: Optional[Dict[str, Any]] = None,
    user_id: str = Depends(get_current_tenant_id),
):
    """Approve recipe and start DuckDB CTE cleaning and verification."""
    job = orchestrator.get_job(job_id, user_id=user_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired.")

    background_tasks.add_task(orchestrator.execute_cleaning_and_verification, job_id, recipe, user_id=user_id)
    return {"status": "started", "job_id": job_id}


@app.get("/api/jobs/{job_id}/download/{format}")
def download_artifact(
    job_id: str,
    format: str,
    user_id: str = Depends(get_current_tenant_id),
):
    """Download cleaned dataset in any supported format (CSV, Parquet, TSV, NDJSON, JSON, XLSX) or verification report."""
    job = orchestrator.get_job(job_id, user_id=user_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired.")

    format_lower = format.lower()

    if format_lower == "report":
        file_path = storage.verification_json_path(job_id, user_id=job.user_id)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Verification report not yet generated.")
        return FileResponse(path=file_path, media_type="application/json", filename=f"verification_{job.id}.json")

    from formats import ExporterRegistry
    if not ExporterRegistry.get(format_lower):
        raise HTTPException(status_code=400, detail=f"Invalid format '{format}'. Supported: csv, tsv, parquet, ndjson, json, xlsx, report.")

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
        else:
            # Multi-part split files (e.g. large Excel) -> package in zip
            import zipfile
            zip_path = storage.job_dir(job_id, user_id=job.user_id) / f"cleaned_{Path(job.filename).stem}_{format_lower}_parts.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in created_paths:
                    zf.write(p, arcname=p.name)
            return FileResponse(
                path=zip_path,
                media_type="application/zip",
                filename=zip_path.name,
            )
    except Exception as e:
        logger.exception("Export failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Export error: {e}")
    except Exception as e:
        logger.exception("Export failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Export error: {e}")


# =========================================================================
# Explore Data (Read-Only Sandboxed SQL Query Box) Endpoints
# =========================================================================

from pydantic import BaseModel, Field
from explore import (
    DataNotFoundError,
    ExploreError,
    QueryNotAllowedError,
    QuerySyntaxError,
    QueryTimeoutError,
    QueryTooLargeError,
    ServerBusyError,
    export_query,
    get_schema,
    run_query,
)


class ExploreQueryRequest(BaseModel):
    sql: str = Field(..., description="Read-only SELECT query")
    row_limit: Optional[int] = Field(None, description="Maximum number of rows to return (capped at 10,000)")


class ExploreExportRequest(BaseModel):
    sql: str = Field(..., description="Query to export to CSV")


@app.get("/api/explore/{job_id}/schema")
def get_explore_schema(
    job_id: str,
    table: str = "data",
    user_id: str = Depends(get_current_tenant_id),
):
    """Retrieve schema, column types, row counts, and sample values for the dataset."""
    try:
        schema = get_schema(job_id=job_id, user_id=user_id, storage=storage.for_user(user_id), table=table)
        return {
            "table": schema.table,
            "row_count": schema.row_count,
            "columns": [{"name": c.name, "type": c.type, "sample_values": c.sample_values} for c in schema.columns],
            "tables_available": schema.tables_available,
        }
    except DataNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (QueryNotAllowedError, QuerySyntaxError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Failed to get explore schema for job %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail="Failed to retrieve dataset schema.")


@app.post("/api/explore/{job_id}/query")
def execute_explore_query(
    job_id: str,
    payload: ExploreQueryRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Execute a validated, read-only SQL query on the job's Parquet dataset."""
    try:
        result = run_query(
            job_id=job_id,
            user_id=user_id,
            sql=payload.sql,
            row_limit=payload.row_limit,
            storage=storage.for_user(user_id),
        )
        return {
            "columns": [{"name": c.name, "type": c.type} for c in result.columns],
            "rows": result.rows,
            "row_count_returned": result.row_count_returned,
            "truncated": result.truncated,
            "elapsed_seconds": result.elapsed_seconds,
            "warnings": result.warnings,
        }
    except DataNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (QueryNotAllowedError, QuerySyntaxError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except QueryTimeoutError as e:
        raise HTTPException(status_code=408, detail=str(e))
    except ServerBusyError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except QueryTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except Exception as e:
        logger.exception("Explore query failed for job %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/explore/{job_id}/export")
def export_explore_query(
    job_id: str,
    payload: ExploreExportRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Export the full result of a SQL query to a downloadable CSV file."""
    try:
        csv_path = export_query(
            job_id=job_id,
            user_id=user_id,
            sql=payload.sql,
            storage=storage.for_user(user_id),
        )
        return FileResponse(
            path=csv_path,
            media_type="text/csv",
            filename=f"query_result_{job_id[:8]}.csv",
        )
    except DataNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (QueryNotAllowedError, QuerySyntaxError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except QueryTimeoutError as e:
        raise HTTPException(status_code=408, detail=str(e))
    except ServerBusyError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except QueryTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except Exception as e:
        logger.exception("Explore export failed for job %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail=str(e))


class AnalystAskRequest(BaseModel):
    question: str = Field(..., description="Natural language question to ask the data analyst")
    user_id: Optional[str] = Field("anonymous", description="User identifier for rate limiting")


@app.post("/api/explore/{job_id}/ask")
def ask_ai_analyst(
    job_id: str,
    payload: AnalystAskRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Ask a natural-language question about the dataset and receive SQL + results."""
    from analyst import (
        ask,
        RateLimitError as AnalystRateLimitError,
        QuestionNotAllowedError,
        LLMUnavailableError,
        LLMTimeoutError,
        LLMBadResponseError,
    )
    try:
        active_uid = user_id if user_id != "default" else (payload.user_id or "default")
        res = ask(
            job_id=job_id,
            user_id=active_uid,
            question=payload.question,
            storage=storage.for_user(user_id),
        )
        return res.to_dict()
    except DataNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except AnalystRateLimitError as e:
        raise HTTPException(status_code=429, detail=e.user_message)
    except QuestionNotAllowedError as e:
        raise HTTPException(status_code=400, detail=e.user_message)
    except (LLMUnavailableError, LLMTimeoutError) as e:
        raise HTTPException(status_code=503, detail=e.user_message)
    except LLMBadResponseError as e:
        raise HTTPException(status_code=502, detail=e.user_message)
    except Exception as e:
        logger.exception("AI Analyst failed for job %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail="Failed to process question with AI Analyst.")


# =========================================================================
# Charts and "Visualize Data" Endpoints
# =========================================================================

from charts import (
    ChartError,
    ChartSpec,
    ChartTimeoutError,
    ChartTooLargeError,
    InvalidChartSpecError,
    build_chart,
    export_chart_data,
    get_column_stats,
    suggest_charts,
    validate_formula,
)
from charts.page_options import PageDataOptions
from charts.tweak import default_starter_spec, get_tweak_options
from charts_ai import (
    auto_visualize,
    chart_from_text,
    plan_visualization,
)
from charts.service import ChartData


class BuildChartRequest(BaseModel):
    spec: Dict[str, Any]
    user_id: Optional[str] = "anonymous"
    page_options: Optional[Dict[str, Any]] = None


class ValidateFormulaRequest(BaseModel):
    formula: str
    name: Optional[str] = None
    user_id: Optional[str] = "anonymous"


class ExportDashboardRequest(BaseModel):
    title: Optional[str] = "Executive Data Intelligence Briefing"
    dataset_name: Optional[str] = "dataset.parquet"
    cards: List[Dict[str, Any]] = []
    metadata: Optional[Dict[str, Any]] = None
    user_id: Optional[str] = "anonymous"


class TweakOptionsRequest(BaseModel):
    spec: Optional[Dict[str, Any]] = None
    user_id: Optional[str] = "anonymous"


class AIPlanRequest(BaseModel):
    user_id: Optional[str] = "anonymous"
    page_options: Optional[Dict[str, Any]] = None
    force_refresh: bool = False


class AIAutoRequest(BaseModel):
    user_id: Optional[str] = "anonymous"
    page_options: Optional[Dict[str, Any]] = None
    max_charts: int = 6
    force_refresh: bool = False


class AIAskRequest(BaseModel):
    request: str
    user_id: Optional[str] = "anonymous"
    page_options: Optional[Dict[str, Any]] = None


@app.get("/api/charts/{job_id}/stats")
def get_dataset_column_stats(job_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Get rich column stats, kind classification, and parse hints for raw datasets."""
    try:
        stats = get_column_stats(job_id=job_id, user_id=user_id, storage=storage.for_user(user_id))
        return {"job_id": job_id, "columns": [s.to_dict() for s in stats]}
    except DataNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Failed to retrieve column stats for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to compute column statistics: {e}")


@app.get("/api/charts/{job_id}/starter-spec")
def get_starter_chart_spec(job_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Generate deterministic starter spec for 'Add chart' button."""
    try:
        stats = get_column_stats(job_id=job_id, user_id=user_id, storage=storage.for_user(user_id))
        schema = {s.name: s.type for s in stats}
        spec = default_starter_spec(schema, stats)
        return {"job_id": job_id, "spec": spec.model_dump()}
    except DataNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Failed to get starter spec for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to get starter spec: {e}")


@app.post("/api/charts/{job_id}/tweak-options")
def get_dataset_tweak_options(
    job_id: str,
    payload: TweakOptionsRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Retrieve allowed types, aggregations, and column options for Tweak panel."""
    try:
        spec_obj = ChartSpec.model_validate(payload.spec) if payload.spec else None
        active_uid = user_id if user_id != "default" else (payload.user_id or "default")
        options = get_tweak_options(
            job_id=job_id,
            user_id=active_uid,
            spec=spec_obj,
            storage=storage.for_user(active_uid),
        )
        return options.model_dump()
    except DataNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Failed to get tweak options for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to get tweak options: {e}")


@app.get("/api/charts/{job_id}/suggest")
def get_chart_suggestions(job_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Get up to 8 prioritized, rule-based chart specifications for the dataset."""
    try:
        suggestions = suggest_charts(job_id=job_id, user_id=user_id, storage=storage.for_user(user_id))
        return {"job_id": job_id, "suggestions": [s.model_dump() for s in suggestions]}
    except DataNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Failed to suggest charts for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to generate chart suggestions: {e}")


@app.post("/api/charts/{job_id}/build")
def build_dataset_chart(
    job_id: str,
    payload: BuildChartRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Compile and execute a ChartSpec against the sandboxed dataset."""
    try:
        spec = ChartSpec.model_validate(payload.spec)
        po = PageDataOptions.model_validate(payload.page_options) if payload.page_options else None
        active_uid = user_id if user_id != "default" else (payload.user_id or "default")
        chart_data = build_chart(
            job_id=job_id,
            user_id=active_uid,
            spec=spec,
            page_options=po,
            storage=storage.for_user(active_uid),
        )
        return chart_data.to_dict()
    except DataNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidChartSpecError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ChartTimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    except ServerBusyError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except Exception as e:
        logger.exception("Failed to build chart for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to execute chart query: {e}")


@app.post("/api/charts/{job_id}/export")
def export_dataset_chart(
    job_id: str,
    payload: BuildChartRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Execute chart aggregation and download the small aggregated data table as CSV."""
    try:
        spec = ChartSpec.model_validate(payload.spec)
        active_uid = user_id if user_id != "default" else (payload.user_id or "default")
        csv_path = export_chart_data(
            job_id=job_id,
            user_id=active_uid,
            spec=spec,
            storage=storage.for_user(active_uid),
        )
        return FileResponse(
            path=csv_path,
            media_type="text/csv",
            filename=f"chart_data_{job_id}.csv",
        )
    except DataNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidChartSpecError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Failed to export chart for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to export chart data: {e}")


@app.post("/api/charts/{job_id}/validate-formula")
def validate_dataset_formula(
    job_id: str,
    payload: ValidateFormulaRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Validate a custom SQL formula against the dataset in DuckDB."""
    try:
        active_uid = user_id if user_id != "default" else (payload.user_id or "default")
        res = validate_formula(
            job_id=job_id,
            formula=payload.formula,
            user_id=active_uid,
            storage=storage.for_user(active_uid),
        )
        return res
    except Exception as e:
        logger.exception("Failed to validate formula for %s: %s", job_id, e)
        return {"valid": False, "inferred_type": None, "sample_result": None, "error": str(e)}


@app.post("/api/charts/{job_id}/export-dashboard-html")
def export_dashboard_html_endpoint(
    job_id: str,
    payload: ExportDashboardRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Generate and return a standalone interactive HTML dashboard report with Plotly."""
    try:
        from charts.dashboard_export import generate_dashboard_html
        html_content = generate_dashboard_html(
            title=payload.title or "Executive Data Intelligence Briefing",
            dataset_name=payload.dataset_name or f"dataset_{job_id}",
            cards_data=payload.cards,
            metadata=payload.metadata,
        )
        return HTMLResponse(
            content=html_content,
            headers={
                "Content-Disposition": f'attachment; filename="dashboard_{job_id}.html"'
            }
        )
    except Exception as e:
        logger.exception("Failed to export dashboard HTML for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to generate dashboard HTML: {e}")


# ============================================================================
# Chart AI Endpoints (Plan, Auto-Visualize, Ask for a Chart)
# ============================================================================

@app.get("/api/charts/{job_id}/ai/status")
def get_chart_ai_status(job_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Return whether Chart AI features are available on this server."""
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    enabled = bool(api_key) and os.getenv("CHARTS_AI_ENABLED", "true").lower() in ("true", "1", "yes")
    return {
        "enabled": enabled,
        "model": os.getenv("CHARTS_AI_MODEL", os.getenv("GEMINI_MODEL", "gemini-2.5-flash")),
    }


@app.post("/api/charts/{job_id}/ai/plan")
def get_visualization_plan(
    job_id: str,
    payload: AIPlanRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Generate AI-assisted visualization plan and advice."""
    try:
        po = PageDataOptions.model_validate(payload.page_options) if payload.page_options else None
        active_uid = user_id if user_id != "default" else (payload.user_id or "default")
        plan = plan_visualization(
            job_id=job_id,
            user_id=active_uid,
            storage=storage.for_user(active_uid),
            page_options=po,
            force_refresh=payload.force_refresh,
        )
        return plan.to_dict()
    except DataNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Failed to generate visualization plan for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to generate visualization plan: {e}")


@app.post("/api/charts/{job_id}/ai/auto")
def auto_visualize_charts(
    job_id: str,
    payload: AIAutoRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Generate and build top AI-recommended charts."""
    try:
        po = PageDataOptions.model_validate(payload.page_options) if payload.page_options else None
        active_uid = user_id if user_id != "default" else (payload.user_id or "default")
        charts = auto_visualize(
            job_id=job_id,
            user_id=active_uid,
            storage=storage.for_user(active_uid),
            page_options=po,
            max_charts=payload.max_charts,
            force_refresh=payload.force_refresh,
        )
        return {
            "items": [
                {
                    "recommendation": rec.to_dict(),
                    "data": d.to_dict() if isinstance(d, ChartData) else None,
                    "error": str(d) if isinstance(d, Exception) else None,
                }
                for rec, d in charts
            ]
        }
    except DataNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Failed to auto-visualize for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to auto-visualize: {e}")


@app.post("/api/charts/{job_id}/ai/ask")
def ask_chart_from_text(
    job_id: str,
    payload: AIAskRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Translate natural-language plain text into a validated chart."""
    try:
        po = PageDataOptions.model_validate(payload.page_options) if payload.page_options else None
        active_uid = user_id if user_id != "default" else (payload.user_id or "default")
        result = chart_from_text(
            job_id=job_id,
            user_id=active_uid,
            request=payload.request,
            storage=storage.for_user(active_uid),
            page_options=po,
        )
        return result.to_dict()
    except DataNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Failed to process chart text request for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to process chart request: {e}")


# ---------------------------------------------------------
# Interactive Data Wrangler & PII Masking API Endpoints
# ---------------------------------------------------------

class WrangleActionRequest(BaseModel):
    action: str
    column: str
    params: Optional[Dict[str, Any]] = Field(default_factory=dict)


class ApplyPiiMaskingRequest(BaseModel):
    columns: Dict[str, Dict[str, Any]]


@app.get("/api/jobs/{job_id}/pii/detect")
def detect_job_pii(job_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Scan dataset columns for sensitive PII entities."""
    from core.privacy.detector import PiiDetector
    from explore.service import resolve_job_parquet_paths, DataNotFoundError

    try:
        user_storage = storage.for_user(user_id)
        clean_pq, orig_pq = resolve_job_parquet_paths(job_id, user_id=user_id, storage=user_storage)
        target_path = clean_pq if clean_pq and clean_pq.exists() else orig_pq
        if not target_path or not target_path.exists():
            raise HTTPException(status_code=404, detail="Dataset not found")

        findings = PiiDetector.scan_dataset(target_path)
        return {
            "job_id": job_id,
            "detected_count": len(findings),
            "findings": [f.to_dict() for f in findings],
        }
    except DataNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Failed PII detection for job %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail=f"Failed PII detection: {e}")


@app.post("/api/jobs/{job_id}/pii/apply")
def apply_job_pii_masking(
    job_id: str,
    payload: ApplyPiiMaskingRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Apply masking transformations to sensitive PII columns."""
    from core.recipe_schema import Recipe, ColumnRule, MaskPiiConfig
    from core.compiler import RecipeCompiler
    from core.wrangler.service import DataWranglerService
    from explore.service import resolve_job_parquet_paths, DataNotFoundError

    try:
        user_storage = storage.for_user(user_id)
        clean_pq, _ = resolve_job_parquet_paths(job_id, user_id=user_id, storage=user_storage)
        if not clean_pq or not clean_pq.exists():
            raise HTTPException(status_code=404, detail="Dataset not found")

        # Ensure wrangle baseline exists
        wrangler = DataWranglerService(storage=user_storage)
        wrangler.ensure_baseline_snapshot(job_id)

        mask_rules = {}
        for col, cfg in payload.columns.items():
            strat = cfg.get("strategy", "partial_mask")
            ent = cfg.get("entity_type")
            mask_rules[col] = ColumnRule(
                mask_pii=MaskPiiConfig(strategy=strat, entity_type=ent)
            )

        recipe = Recipe(columns=mask_rules)
        compiler = RecipeCompiler()

        # Execute into temporary file then swap
        temp_pq = clean_pq.parent / f".temp_masked_{job_id[:8]}.parquet"
        compiler.execute_clean(recipe, clean_pq, temp_pq)
        if temp_pq.exists():
            shutil.move(temp_pq, clean_pq)

        # Update CSV
        clean_csv = user_storage.clean_csv_path(job_id, user_id=user_id)
        c_conn = duckdb.connect(":memory:")
        c_conn.execute(
            f"COPY (SELECT * FROM read_parquet('{clean_pq.as_posix()}')) "
            f"TO '{clean_csv.as_posix()}' (HEADER, DELIMITER ',');"
        )
        c_conn.close()

        row_count, cols, preview = wrangler._get_dataset_info(clean_pq)
        return {
            "success": True,
            "message": f"Successfully masked {len(payload.columns)} PII columns.",
            "row_count": row_count,
            "columns": cols,
            "preview_rows": preview,
        }
    except DataNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Failed applying PII masking for job %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail=f"Failed applying PII masking: {e}")


@app.post("/api/jobs/{job_id}/wrangle")
def apply_wrangle_action(
    job_id: str,
    payload: WrangleActionRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Execute point-and-click cell/column wrangle mutation."""
    from core.wrangler.service import DataWranglerService

    try:
        user_storage = storage.for_user(user_id)
        wrangler = DataWranglerService(storage=user_storage)
        result = wrangler.apply_action(
            job_id=job_id,
            action_type=payload.action,
            column=payload.column,
            params=payload.params,
        )

        # Synchronize CSV export
        clean_pq = user_storage.clean_parquet_path(job_id, user_id=user_id)
        clean_csv = user_storage.clean_csv_path(job_id, user_id=user_id)
        if clean_pq.exists():
            c_conn = duckdb.connect(":memory:")
            c_conn.execute(
                f"COPY (SELECT * FROM read_parquet('{clean_pq.as_posix()}')) "
                f"TO '{clean_csv.as_posix()}' (HEADER, DELIMITER ',');"
            )
            c_conn.close()

        return result.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Wrangle execution failed for job %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail=f"Failed wrangling dataset: {e}")


@app.post("/api/jobs/{job_id}/wrangle/undo")
def undo_wrangle_action(job_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Roll back the last wrangle mutation."""
    from core.wrangler.service import DataWranglerService

    try:
        user_storage = storage.for_user(user_id)
        wrangler = DataWranglerService(storage=user_storage)
        result = wrangler.undo(job_id=job_id)

        clean_pq = user_storage.clean_parquet_path(job_id, user_id=user_id)
        clean_csv = user_storage.clean_csv_path(job_id, user_id=user_id)
        if clean_pq.exists():
            c_conn = duckdb.connect(":memory:")
            c_conn.execute(
                f"COPY (SELECT * FROM read_parquet('{clean_pq.as_posix()}')) "
                f"TO '{clean_csv.as_posix()}' (HEADER, DELIMITER ',');"
            )
            c_conn.close()

        return result.to_dict()
    except Exception as e:
        logger.exception("Wrangle undo failed for job %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to undo wrangle action: {e}")


@app.post("/api/jobs/{job_id}/wrangle/reset")
def reset_wrangle_actions(job_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Revert dataset to step 0 baseline."""
    from core.wrangler.service import DataWranglerService

    try:
        user_storage = storage.for_user(user_id)
        wrangler = DataWranglerService(storage=user_storage)
        result = wrangler.reset(job_id=job_id)

        clean_pq = user_storage.clean_parquet_path(job_id, user_id=user_id)
        clean_csv = user_storage.clean_csv_path(job_id, user_id=user_id)
        if clean_pq.exists():
            c_conn = duckdb.connect(":memory:")
            c_conn.execute(
                f"COPY (SELECT * FROM read_parquet('{clean_pq.as_posix()}')) "
                f"TO '{clean_csv.as_posix()}' (HEADER, DELIMITER ',');"
            )
            c_conn.close()

        return result.to_dict()
    except Exception as e:
        logger.exception("Wrangle reset failed for job %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to reset wrangle actions: {e}")


@app.get("/api/jobs/{job_id}/wrangle/history")
def get_wrangle_history(job_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Get list of applied wrangle actions for a job."""
    from core.wrangler.service import DataWranglerService

    try:
        user_storage = storage.for_user(user_id)
        wrangler = DataWranglerService(storage=user_storage)
        history = wrangler.get_history(job_id=job_id)
        return {
            "job_id": job_id,
            "history": history,
            "can_undo": len(history) > 0,
        }
    except Exception as e:
        logger.exception("Failed to get wrangle history for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to get wrangle history: {e}")


# ==============================================================================
# Multi-Table Staging & ERD Visualizer Endpoints
# ==============================================================================
from core.modeling.service import ModelingService
from core.modeling.models import Relationship

modeling_service = ModelingService()


@app.post("/api/modeling/sessions")
def create_modeling_session(user_id: str = Depends(get_current_tenant_id)):
    """Create a new multi-table modeling session."""
    session_id = modeling_service.create_session(user_id=user_id)
    state = modeling_service.get_session_state(session_id, user_id=user_id)
    return state.model_dump()


@app.get("/api/modeling/sessions/{session_id}")
def get_modeling_session(session_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Retrieve full session state (staged tables, columns, relationships)."""
    try:
        state = modeling_service.get_session_state(session_id, user_id=user_id)
        return state.model_dump()
    except (PermissionError, KeyError):
        raise HTTPException(status_code=404, detail="Modeling session not found or expired.")
    except Exception as e:
        logger.exception("Failed to retrieve modeling session %s: %s", session_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/modeling/sessions/{session_id}/tables")
async def upload_modeling_table(
    session_id: str,
    file: UploadFile = File(...),
    table_name: Optional[str] = Form(None),
    user_id: str = Depends(get_current_tenant_id),
):
    """Upload a CSV, Parquet, JSON, Excel, or SQLite file into the modeling session."""
    import tempfile
    suffix = Path(file.filename or "upload").suffix.lower()

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        if suffix in (".db", ".sqlite", ".sqlite3"):
            modeling_service.add_sqlite_database(session_id, tmp_path, user_id=user_id)
        else:
            modeling_service.add_table(
                session_id=session_id,
                file_path=tmp_path,
                original_filename=file.filename or "table",
                table_name=table_name,
                user_id=user_id,
            )

        state = modeling_service.get_session_state(session_id, user_id=user_id)
        return state.model_dump()
    except (PermissionError, KeyError):
        raise HTTPException(status_code=404, detail="Modeling session not found or expired.")
    except Exception as e:
        logger.exception("Failed to add table to session %s: %s", session_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to add table: {e}")
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


@app.delete("/api/modeling/sessions/{session_id}/tables/{table_name}")
def delete_modeling_table(session_id: str, table_name: str, user_id: str = Depends(get_current_tenant_id)):
    """Remove a staged table and its linked relationships from the session."""
    try:
        modeling_service.remove_table(session_id, table_name, user_id=user_id)
        state = modeling_service.get_session_state(session_id, user_id=user_id)
        return state.model_dump()
    except (PermissionError, KeyError):
        raise HTTPException(status_code=404, detail="Modeling session not found or expired.")
    except Exception as e:
        logger.exception("Failed to remove table %s from session %s: %s", table_name, session_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/modeling/sessions/{session_id}/tables/{table_name}/position")
async def update_modeling_table_position(
    session_id: str,
    table_name: str,
    request: Request,
    user_id: str = Depends(get_current_tenant_id),
):
    """Save canvas coordinates for a table entity card."""
    try:
        body = await request.json()
        x = float(body.get("x", 0.0))
        y = float(body.get("y", 0.0))
        modeling_service.update_table_position(session_id, table_name, x, y, user_id=user_id)
        return {"status": "ok"}
    except (PermissionError, KeyError):
        raise HTTPException(status_code=404, detail="Modeling session not found or expired.")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/modeling/sessions/{session_id}/detect")
def detect_modeling_relationships(session_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Re-run primary key and foreign key overlap detection."""
    try:
        modeling_service.detect_relationships(session_id, user_id=user_id)
        state = modeling_service.get_session_state(session_id, user_id=user_id)
        return state.model_dump()
    except (PermissionError, KeyError):
        raise HTTPException(status_code=404, detail="Modeling session not found or expired.")
    except Exception as e:
        logger.exception("Failed to detect relationships for session %s: %s", session_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/modeling/sessions/{session_id}/relationships")
async def save_modeling_relationship(session_id: str, request: Request, user_id: str = Depends(get_current_tenant_id)):
    """Create or update a relationship."""
    try:
        body = await request.json()
        rel = Relationship(**body)
        saved = modeling_service.add_or_update_relationship(session_id, rel, user_id=user_id)
        return saved.model_dump()
    except (PermissionError, KeyError):
        raise HTTPException(status_code=404, detail="Modeling session not found or expired.")
    except Exception as e:
        logger.exception("Failed to save relationship for session %s: %s", session_id, e)
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/modeling/sessions/{session_id}/relationships/{rel_id}")
def delete_modeling_relationship(session_id: str, rel_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Delete a relationship by ID."""
    try:
        modeling_service.delete_relationship(session_id, rel_id, user_id=user_id)
        return {"status": "ok", "deleted": rel_id}
    except (PermissionError, KeyError):
        raise HTTPException(status_code=404, detail="Modeling session not found or expired.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/modeling/sessions/{session_id}/preview-join")
async def preview_modeling_join(session_id: str, request: Request, user_id: str = Depends(get_current_tenant_id)):
    """Preview a JOIN between two tables and inspect match stats."""
    try:
        body = await request.json()
        result = modeling_service.preview_join(
            session_id=session_id,
            from_table=body["from_table"],
            from_column=body["from_column"],
            to_table=body["to_table"],
            to_column=body["to_column"],
            join_type=body.get("join_type", "INNER"),
            user_id=user_id,
        )
        return result.model_dump()
    except (PermissionError, KeyError):
        raise HTTPException(status_code=404, detail="Modeling session not found or expired.")
    except Exception as e:
        logger.exception("Failed to preview join for session %s: %s", session_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/modeling/sessions/{session_id}/export")
def export_modeling_schema(session_id: str, format: str = "mermaid", user_id: str = Depends(get_current_tenant_id)):
    """Export relational schema as Mermaid ERD, DBML, or SQL DDL."""
    try:
        content = modeling_service.export_schema(session_id, export_format=format, user_id=user_id)
        return {"format": format, "content": content}
    except (PermissionError, KeyError):
        raise HTTPException(status_code=404, detail="Modeling session not found or expired.")
    except Exception as e:
        logger.exception("Failed to export schema for session %s: %s", session_id, e)
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/modeling/sessions/{session_id}/handoff-query")
def handoff_modeling_to_query(session_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Prepare a pre-composed SQL JOIN query for the SQL Query Box."""
    try:
        handoff = modeling_service.create_query_handoff(session_id, user_id=user_id)
        return handoff
    except (PermissionError, KeyError):
        raise HTTPException(status_code=404, detail="Modeling session not found or expired.")
    except Exception as e:
        logger.exception("Failed to create query handoff for session %s: %s", session_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/modeling/sessions/{session_id}/export-csv")
def export_modeling_merged_csv(session_id: str, rel_id: Optional[str] = None, user_id: str = Depends(get_current_tenant_id)):
    """Execute JOIN and return merged CSV download from DuckDB."""
    import tempfile
    from fastapi.responses import FileResponse
    try:
        tmp_csv = Path(tempfile.mktemp(suffix=".csv"))
        modeling_service.export_joined_csv(session_id, tmp_csv, join_rel_id=rel_id, user_id=user_id)
        filename = f"merged_{session_id}.csv" if not rel_id else f"join_{rel_id}.csv"
        return FileResponse(
            path=str(tmp_csv),
            media_type="text/csv",
            filename=filename,
        )
    except (PermissionError, KeyError):
        raise HTTPException(status_code=404, detail="Modeling session not found or expired.")
    except Exception as e:
        logger.exception("Failed to export merged CSV for session %s: %s", session_id, e)
        raise HTTPException(status_code=500, detail=str(e))



# Mount built frontend assets if present
frontend_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if frontend_dist.exists():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")


