"""Charting, dashboard HTML export, and Chart AI endpoints."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from app.dependencies import get_storage
from charts import (
    ChartSpec,
    ChartTimeoutError,
    InvalidChartSpecError,
    build_chart,
    export_chart_data,
    get_column_stats,
    suggest_charts,
    validate_formula,
)
from charts.page_options import PageDataOptions
from charts.service import ChartData
from charts.tweak import default_starter_spec, get_tweak_options
from charts_ai import auto_visualize, chart_from_text, plan_visualization
from core.security import validate_safe_id
from core.tenant import get_current_tenant_id, resolve_active_tenant_id
from explore import DataNotFoundError, ServerBusyError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/charts", tags=["Charts & Visualize Data"])


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


@router.get("/{job_id}/stats")
def get_dataset_column_stats(job_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Get rich column stats, kind classification, and parse hints for raw datasets."""
    storage = get_storage()
    try:
        stats = get_column_stats(job_id=job_id, user_id=user_id, storage=storage.for_user(user_id))
        return {"job_id": job_id, "columns": [s.to_dict() for s in stats]}
    except DataNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to retrieve column stats for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail="Failed to compute column statistics.")


@router.get("/{job_id}/starter-spec")
def get_starter_chart_spec(job_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Generate deterministic starter spec for 'Add chart' button."""
    storage = get_storage()
    try:
        stats = get_column_stats(job_id=job_id, user_id=user_id, storage=storage.for_user(user_id))
        schema = {s.name: s.type for s in stats}
        spec = default_starter_spec(schema, stats)
        return {"job_id": job_id, "spec": spec.model_dump()}
    except DataNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to get starter spec for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail="Failed to get starter spec.")


@router.post("/{job_id}/tweak-options")
def get_dataset_tweak_options(
    job_id: str,
    payload: TweakOptionsRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Retrieve allowed types, aggregations, and column options for Tweak panel."""
    storage = get_storage()
    try:
        spec_obj = ChartSpec.model_validate(payload.spec) if payload.spec else None
        active_uid = resolve_active_tenant_id(user_id, payload.user_id)
        options = get_tweak_options(
            job_id=job_id,
            user_id=active_uid,
            spec=spec_obj,
            storage=storage.for_user(active_uid),
        )
        return options.model_dump()
    except DataNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to get tweak options for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail="Failed to get tweak options.")


@router.get("/{job_id}/suggest")
def get_chart_suggestions(job_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Get up to 8 prioritized, rule-based chart specifications for the dataset."""
    storage = get_storage()
    try:
        suggestions = suggest_charts(job_id=job_id, user_id=user_id, storage=storage.for_user(user_id))
        return {"job_id": job_id, "suggestions": [s.model_dump() for s in suggestions]}
    except DataNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to suggest charts for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail="Failed to generate chart suggestions.")


@router.post("/{job_id}/build")
def build_dataset_chart(
    job_id: str,
    payload: BuildChartRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Compile and execute a ChartSpec against the sandboxed dataset."""
    storage = get_storage()
    try:
        spec = ChartSpec.model_validate(payload.spec)
        po = PageDataOptions.model_validate(payload.page_options) if payload.page_options else None
        active_uid = resolve_active_tenant_id(user_id, payload.user_id)
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
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to build chart for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail="Failed to execute chart query.")


@router.post("/{job_id}/export")
def export_dataset_chart(
    job_id: str,
    payload: BuildChartRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Execute chart aggregation and download the small aggregated data table as CSV."""
    storage = get_storage()
    try:
        safe_job_id = validate_safe_id(job_id, label="job_id")
        spec = ChartSpec.model_validate(payload.spec)
        active_uid = resolve_active_tenant_id(user_id, payload.user_id)
        csv_path = export_chart_data(
            job_id=safe_job_id,
            user_id=active_uid,
            spec=spec,
            storage=storage.for_user(active_uid),
        )
        return FileResponse(
            path=csv_path,
            media_type="text/csv",
            filename=f"chart_data_{safe_job_id}.csv",
        )
    except DataNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidChartSpecError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to export chart for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail="Failed to export chart data.")


@router.post("/{job_id}/validate-formula")
def validate_dataset_formula(
    job_id: str,
    payload: ValidateFormulaRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Validate a custom SQL formula against the dataset in DuckDB."""
    storage = get_storage()
    try:
        active_uid = resolve_active_tenant_id(user_id, payload.user_id)
        return validate_formula(
            job_id=job_id,
            formula=payload.formula,
            user_id=active_uid,
            storage=storage.for_user(active_uid),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to validate formula for %s: %s", job_id, e)
        return {"valid": False, "inferred_type": None, "sample_result": None, "error": str(e)}


@router.post("/{job_id}/export-dashboard-html")
def export_dashboard_html_endpoint(
    job_id: str,
    payload: ExportDashboardRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Generate and return a standalone interactive HTML dashboard report with Plotly."""
    try:
        from charts.dashboard_export import generate_dashboard_html

        safe_job_id = validate_safe_id(job_id, label="job_id")
        html_content = generate_dashboard_html(
            title=payload.title or "Executive Data Intelligence Briefing",
            dataset_name=payload.dataset_name or f"dataset_{safe_job_id}",
            cards_data=payload.cards,
            metadata=payload.metadata,
        )
        return HTMLResponse(
            content=html_content,
            headers={
                "Content-Disposition": f'attachment; filename="dashboard_{safe_job_id}.html"'
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to export dashboard HTML for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail="Failed to generate dashboard HTML.")


@router.get("/{job_id}/ai/status")
def get_chart_ai_status(job_id: str, user_id: str = Depends(get_current_tenant_id)):
    """Return whether Chart AI features are available on this server."""
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    enabled = bool(api_key) and os.getenv("CHARTS_AI_ENABLED", "true").lower() in ("true", "1", "yes")
    return {
        "enabled": enabled,
        "model": os.getenv("CHARTS_AI_MODEL", os.getenv("GEMINI_MODEL", "gemini-2.5-flash")),
    }


@router.post("/{job_id}/ai/plan")
def get_visualization_plan(
    job_id: str,
    payload: AIPlanRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Generate AI-assisted visualization plan and advice."""
    storage = get_storage()
    try:
        po = PageDataOptions.model_validate(payload.page_options) if payload.page_options else None
        active_uid = resolve_active_tenant_id(user_id, payload.user_id)
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
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to generate visualization plan for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail="Failed to generate visualization plan.")


@router.post("/{job_id}/ai/auto")
def auto_visualize_charts(
    job_id: str,
    payload: AIAutoRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Generate and build top AI-recommended charts."""
    storage = get_storage()
    try:
        po = PageDataOptions.model_validate(payload.page_options) if payload.page_options else None
        active_uid = resolve_active_tenant_id(user_id, payload.user_id)
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
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to auto-visualize for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail="Failed to auto-visualize charts.")


@router.post("/{job_id}/ai/ask")
def ask_chart_from_text(
    job_id: str,
    payload: AIAskRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Translate natural-language plain text into a validated chart."""
    storage = get_storage()
    try:
        po = PageDataOptions.model_validate(payload.page_options) if payload.page_options else None
        active_uid = resolve_active_tenant_id(user_id, payload.user_id)
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
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to process chart text request for %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail="Failed to process chart request.")

