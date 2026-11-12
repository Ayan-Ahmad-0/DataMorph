"""FastAPI router package exposing domain-scoped route modules."""

from __future__ import annotations

from app.routers.charts import router as charts_router
from app.routers.convert import router as convert_router
from app.routers.explore import router as explore_router
from app.routers.jobs import router as jobs_router
from app.routers.modeling import router as modeling_router
from app.routers.system import router as system_router
from app.routers.wrangler_pii import router as wrangler_pii_router

__all__ = [
    "system_router",
    "convert_router",
    "jobs_router",
    "explore_router",
    "charts_router",
    "wrangler_pii_router",
    "modeling_router",
]

