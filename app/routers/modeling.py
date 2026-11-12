"""Multi-table staging, relationship detection, join preview, and ERD export routes."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask as StarletteBackgroundTask
from starlette.concurrency import run_in_threadpool

from app.dependencies import copy_stream_to_path, get_max_upload_bytes, get_modeling_service
from core.modeling.models import Relationship
from core.security import sanitize_filename, validate_safe_id
from core.tenant import get_current_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/modeling", tags=["Multi-Table Modeling & ERD"])


@router.post("/sessions")
def create_modeling_session(user_id: str = Depends(get_current_tenant_id)):
    """Create a new multi-table modeling session."""
    modeling_service = get_modeling_service()
    session_id = modeling_service.create_session(user_id=user_id)
    state = modeling_service.get_session_state(session_id, user_id=user_id)
    return state.model_dump()


@router.get("/sessions/{session_id}")
def get_modeling_session(session_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Retrieve full session state (staged tables, columns, relationships)."""
    modeling_service = get_modeling_service()
    try:
        state = modeling_service.get_session_state(session_id, user_id=user_id)
        return state.model_dump()
    except (PermissionError, KeyError):
        raise HTTPException(status_code=404, detail="Modeling session not found or expired.")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to retrieve modeling session %s: %s", session_id, e)
        raise HTTPException(status_code=500, detail="Failed to retrieve modeling session.")


@router.post("/sessions/{session_id}/tables")
async def upload_modeling_table(
    session_id: str,
    file: UploadFile = File(...),
    table_name: Optional[str] = Form(None),
    user_id: str = Depends(get_current_tenant_id),
):
    """Upload a CSV, Parquet, JSON, Excel, or SQLite file into the modeling session."""
    modeling_service = get_modeling_service()
    safe_filename = sanitize_filename(file.filename, fallback="table.csv")
    suffix = Path(safe_filename).suffix.lower()

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        await run_in_threadpool(copy_stream_to_path, file.file, tmp_path, get_max_upload_bytes())
        if suffix in (".db", ".sqlite", ".sqlite3"):
            modeling_service.add_sqlite_database(session_id, tmp_path, user_id=user_id)
        else:
            modeling_service.add_table(
                session_id=session_id,
                file_path=tmp_path,
                original_filename=safe_filename,
                table_name=table_name,
                user_id=user_id,
            )

        state = modeling_service.get_session_state(session_id, user_id=user_id)
        return state.model_dump()
    except (PermissionError, KeyError):
        raise HTTPException(status_code=404, detail="Modeling session not found or expired.")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to add table to session %s: %s", session_id, e)
        raise HTTPException(status_code=500, detail="Failed to stage uploaded modeling table.")
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


@router.delete("/sessions/{session_id}/tables/{table_name}")
def delete_modeling_table(session_id: str, table_name: str, user_id: str = Depends(get_current_tenant_id)):
    """Remove a staged table and its linked relationships from the session."""
    modeling_service = get_modeling_service()
    try:
        modeling_service.remove_table(session_id, table_name, user_id=user_id)
        state = modeling_service.get_session_state(session_id, user_id=user_id)
        return state.model_dump()
    except (PermissionError, KeyError):
        raise HTTPException(status_code=404, detail="Modeling session not found or expired.")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to remove table %s from session %s: %s", table_name, session_id, e)
        raise HTTPException(status_code=500, detail="Failed to remove modeling table.")


@router.post("/sessions/{session_id}/tables/{table_name}/position")
async def update_modeling_table_position(
    session_id: str,
    table_name: str,
    request: Request,
    user_id: str = Depends(get_current_tenant_id),
):
    """Save canvas coordinates for a table entity card."""
    modeling_service = get_modeling_service()
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


@router.post("/sessions/{session_id}/detect")
def detect_modeling_relationships(session_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Re-run primary key and foreign key overlap detection."""
    modeling_service = get_modeling_service()
    try:
        modeling_service.detect_relationships(session_id, user_id=user_id)
        state = modeling_service.get_session_state(session_id, user_id=user_id)
        return state.model_dump()
    except (PermissionError, KeyError):
        raise HTTPException(status_code=404, detail="Modeling session not found or expired.")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to detect relationships for session %s: %s", session_id, e)
        raise HTTPException(status_code=500, detail="Failed to detect table relationships.")


@router.post("/sessions/{session_id}/relationships")
async def save_modeling_relationship(session_id: str, request: Request, user_id: str = Depends(get_current_tenant_id)):
    """Create or update a relationship."""
    modeling_service = get_modeling_service()
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


@router.delete("/sessions/{session_id}/relationships/{rel_id}")
def delete_modeling_relationship(session_id: str, rel_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Delete a relationship by ID."""
    validate_safe_id(rel_id, label="rel_id")
    modeling_service = get_modeling_service()
    try:
        modeling_service.delete_relationship(session_id, rel_id, user_id=user_id)
        return {"status": "ok", "deleted": rel_id}
    except (PermissionError, KeyError):
        raise HTTPException(status_code=404, detail="Modeling session not found or expired.")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to delete relationship %s for session %s: %s", rel_id, session_id, e)
        raise HTTPException(status_code=500, detail="Failed to delete relationship.")


@router.post("/sessions/{session_id}/preview-join")
async def preview_modeling_join(session_id: str, request: Request, user_id: str = Depends(get_current_tenant_id)):
    """Preview a JOIN between two tables and inspect match stats."""
    modeling_service = get_modeling_service()
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
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to preview join for session %s: %s", session_id, e)
        raise HTTPException(status_code=500, detail="Failed to preview join.")


@router.get("/sessions/{session_id}/export")
def export_modeling_schema(session_id: str, format: str = "mermaid", user_id: str = Depends(get_current_tenant_id)):
    """Export relational schema as Mermaid ERD, DBML, or SQL DDL."""
    modeling_service = get_modeling_service()
    try:
        content = modeling_service.export_schema(session_id, export_format=format, user_id=user_id)
        return {"format": format, "content": content}
    except (PermissionError, KeyError):
        raise HTTPException(status_code=404, detail="Modeling session not found or expired.")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to export schema for session %s: %s", session_id, e)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/sessions/{session_id}/handoff-query")
def handoff_modeling_to_query(session_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Prepare a pre-composed SQL JOIN query for the SQL Query Box."""
    modeling_service = get_modeling_service()
    try:
        return modeling_service.create_query_handoff(session_id, user_id=user_id)
    except (PermissionError, KeyError):
        raise HTTPException(status_code=404, detail="Modeling session not found or expired.")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to create query handoff for session %s: %s", session_id, e)
        raise HTTPException(status_code=500, detail="Failed to create query handoff.")


@router.get("/sessions/{session_id}/export-csv")
def export_modeling_merged_csv(session_id: str, rel_id: Optional[str] = None, user_id: str = Depends(get_current_tenant_id)):
    """Execute JOIN and return merged CSV download from DuckDB."""
    if rel_id is not None:
        validate_safe_id(rel_id, label="rel_id")
    modeling_service = get_modeling_service()
    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tf:
            tmp_path = Path(tf.name)
        modeling_service.export_joined_csv(session_id, tmp_path, join_rel_id=rel_id, user_id=user_id)
        safe_sid = validate_safe_id(session_id, label="session_id")
        filename = f"merged_{safe_sid}.csv" if not rel_id else f"join_{rel_id}.csv"
        return FileResponse(
            path=str(tmp_path),
            media_type="text/csv",
            filename=filename,
            background=StarletteBackgroundTask(tmp_path.unlink, missing_ok=True),
        )
    except (PermissionError, KeyError):
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=404, detail="Modeling session not found or expired.")
    except HTTPException:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise
    except Exception as e:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        logger.exception("Failed to export merged CSV for session %s: %s", session_id, e)
        raise HTTPException(status_code=500, detail="Failed to export merged CSV.")

