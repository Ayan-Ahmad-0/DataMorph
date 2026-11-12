"""Read-only sandboxed SQL exploration and AI Analyst routes."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.dependencies import get_storage
from core.tenant import get_current_tenant_id
from explore import (
    DataNotFoundError,
    QueryNotAllowedError,
    QuerySyntaxError,
    QueryTimeoutError,
    QueryTooLargeError,
    ServerBusyError,
    export_query,
    get_schema,
    run_query,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/explore", tags=["Explore & AI Analyst"])


class ExploreQueryRequest(BaseModel):
    sql: str = Field(..., description="Read-only SELECT query")
    row_limit: Optional[int] = Field(None, description="Maximum number of rows to return (capped at 10,000)")


class ExploreExportRequest(BaseModel):
    sql: str = Field(..., description="Query to export to CSV")


class AnalystAskRequest(BaseModel):
    question: str = Field(..., description="Natural language question to ask the data analyst")
    user_id: Optional[str] = Field("anonymous", description="User identifier for rate limiting")


@router.get("/{job_id}/schema")
def get_explore_schema(
    job_id: str,
    table: str = "data",
    user_id: str = Depends(get_current_tenant_id),
):
    """Retrieve schema, column types, row counts, and sample values for the dataset."""
    storage = get_storage()
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


@router.post("/{job_id}/query")
def execute_explore_query(
    job_id: str,
    payload: ExploreQueryRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Execute a validated, read-only SQL query on the job's Parquet dataset."""
    storage = get_storage()
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


@router.post("/{job_id}/export")
def export_explore_query(
    job_id: str,
    payload: ExploreExportRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Export the full result of a SQL query to a downloadable CSV file."""
    storage = get_storage()
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


@router.post("/{job_id}/ask")
def ask_ai_analyst(
    job_id: str,
    payload: AnalystAskRequest,
    user_id: str = Depends(get_current_tenant_id),
):
    """Ask a natural-language question about the dataset and receive SQL + results."""
    from analyst import (
        LLMBadResponseError,
        LLMTimeoutError,
        LLMUnavailableError,
        QuestionNotAllowedError,
        RateLimitError as AnalystRateLimitError,
        ask,
    )

    storage = get_storage()
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
