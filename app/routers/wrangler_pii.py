"""Interactive Data Wrangler and PII Masking routes."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_storage
from core.security import escape_sql_literal, validate_safe_id
from core.tenant import get_current_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["Data Wrangler & PII Masking"])


class WrangleActionRequest(BaseModel):
    action: str
    column: str
    params: Optional[Dict[str, Any]] = Field(default_factory=dict)


class ApplyPiiMaskingRequest(BaseModel):
    columns: Dict[str, Dict[str, Any]]


def _sync_clean_csv(clean_pq: Path, clean_csv: Path) -> None:
    """Export updated clean Parquet snapshot to CSV cleanly."""
    if not clean_pq.exists():
        return
    c_conn = duckdb.connect(":memory:")
    try:
        src = escape_sql_literal(clean_pq.as_posix())
        dst = escape_sql_literal(clean_csv.as_posix())
        c_conn.execute(
            f"COPY (SELECT * FROM read_parquet('{src}')) "
            f"TO '{dst}' (HEADER, DELIMITER ',');"
        )
    finally:
        c_conn.close()


@router.get("/{job_id}/pii/detect")
def detect_job_pii(job_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Scan dataset columns for sensitive PII entities."""
    from core.privacy.detector import PiiDetector
    from explore.service import DataNotFoundError, resolve_job_parquet_paths

    storage = get_storage()
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
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed PII detection for job %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail="Failed PII detection due to an internal error.")


@router.post("/{job_id}/pii/apply")
def apply_job_pii_masking(
    job_id: str,
    payload: ApplyPiiMaskingRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Apply masking transformations to sensitive PII columns."""
    from core.compiler import RecipeCompiler
    from core.recipe_schema import ColumnRule, MaskPiiConfig, Recipe
    from core.wrangler.service import DataWranglerService
    from explore.service import DataNotFoundError, resolve_job_parquet_paths

    storage = get_storage()
    try:
        user_storage = storage.for_user(user_id)
        clean_pq, _ = resolve_job_parquet_paths(job_id, user_id=user_id, storage=user_storage)
        if not clean_pq or not clean_pq.exists():
            raise HTTPException(status_code=404, detail="Dataset not found")

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

        temp_pq = clean_pq.parent / f".temp_masked_{validate_safe_id(job_id, label='job_id')[:8]}.parquet"
        compiler.execute_clean(recipe, clean_pq, temp_pq)
        if temp_pq.exists():
            shutil.move(temp_pq, clean_pq)

        _sync_clean_csv(clean_pq, user_storage.clean_csv_path(job_id, user_id=user_id))

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
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed applying PII masking for job %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail="Failed applying PII masking due to an internal error.")


@router.post("/{job_id}/wrangle")
def apply_wrangle_action(
    job_id: str,
    payload: WrangleActionRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Execute point-and-click cell/column wrangle mutation."""
    from core.wrangler.service import DataWranglerService

    storage = get_storage()
    try:
        user_storage = storage.for_user(user_id)
        wrangler = DataWranglerService(storage=user_storage)
        result = wrangler.apply_action(
            job_id=job_id,
            action_type=payload.action,
            column=payload.column,
            params=payload.params,
        )
        _sync_clean_csv(
            user_storage.clean_parquet_path(job_id, user_id=user_id),
            user_storage.clean_csv_path(job_id, user_id=user_id),
        )
        return result.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Wrangle execution failed for job %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail="Failed wrangling dataset due to an internal error.")


@router.post("/{job_id}/wrangle/undo")
def undo_wrangle_action(job_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Roll back the last wrangle mutation."""
    from core.wrangler.service import DataWranglerService

    storage = get_storage()
    try:
        user_storage = storage.for_user(user_id)
        wrangler = DataWranglerService(storage=user_storage)
        result = wrangler.undo(job_id=job_id)
        _sync_clean_csv(
            user_storage.clean_parquet_path(job_id, user_id=user_id),
            user_storage.clean_csv_path(job_id, user_id=user_id),
        )
        return result.to_dict()
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Wrangle undo failed for job %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail="Failed to undo wrangle action.")


@router.post("/{job_id}/wrangle/reset")
def reset_wrangle_actions(job_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Revert dataset to step 0 baseline."""
    from core.wrangler.service import DataWranglerService

    storage = get_storage()
    try:
        user_storage = storage.for_user(user_id)
        wrangler = DataWranglerService(storage=user_storage)
        result = wrangler.reset(job_id=job_id)
        _sync_clean_csv(
            user_storage.clean_parquet_path(job_id, user_id=user_id),
            user_storage.clean_csv_path(job_id, user_id=user_id),
        )
        return result.to_dict()
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Wrangle reset failed for job %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail="Failed to reset wrangle actions.")


@router.get("/{job_id}/wrangle/history")
def get_wrangle_history(job_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Get list of applied wrangle actions for a job."""
    from core.wrangler.service import DataWranglerService

    storage = get_storage()
    try:
        user_storage = storage.for_user(user_id)
        wrangler = DataWranglerService(storage=user_storage)
        history = wrangler.get_history(job_id=job_id)
        return {
            "job_id": job_id,
            "history": history,
            "can_undo": len(history) > 0,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to get wrangle history for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail="Failed to get wrangle history.")

