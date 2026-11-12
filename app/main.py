"""FastAPI application entrypoint wiring middleware, routers, lifespan, and static SPA."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.orchestrator import get_orchestrator
from app.routers import (
    charts_router,
    convert_router,
    explore_router,
    jobs_router,
    modeling_router,
    system_router,
    wrangler_pii_router,
)
from app.storage import JobStorage
from connectors import connectors_router
from core.modeling.service import ModelingService
from core.retention import StorageRetentionManager, retention_daemon_loop

logger = logging.getLogger(__name__)

storage = JobStorage()
orchestrator = get_orchestrator(storage=storage)
retention_manager = StorageRetentionManager()
convert_jobs: Dict[str, Any] = {}
modeling_service = ModelingService()


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


@app.middleware("http")
async def add_cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path in ("/", "/index.html") or path.endswith(".html"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    elif path.startswith("/assets/") and response.status_code == 200:
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


# Compress JSON/text/JS/CSS responses automatically while leaving small payloads untouched
app.add_middleware(GZipMiddleware, minimum_size=1024)

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

# Register domain API routers
app.include_router(connectors_router)
app.include_router(system_router)
app.include_router(convert_router)
app.include_router(jobs_router)
app.include_router(explore_router)
app.include_router(charts_router)
app.include_router(wrangler_pii_router)
app.include_router(modeling_router)

# Mount built frontend assets if present
frontend_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if frontend_dist.exists():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")
