"""System health and storage retention maintenance routes."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter

from app.dependencies import get_convert_jobs, get_orchestrator_dep, get_retention_manager

router = APIRouter(prefix="/api", tags=["System & Maintenance"])


@router.get("/health")
def health_check():
    return {"status": "ok", "engine": "DuckDB", "service": "data-cleaner-api"}


@router.post("/maintenance/cleanup")
def trigger_storage_cleanup(max_age_hours: Optional[float] = None):
    """Trigger an on-demand retention sweep across memory and dynamic storage directories."""
    retention_manager = get_retention_manager()
    result = retention_manager.purge_expired_data(
        orchestrator=get_orchestrator_dep(),
        convert_jobs=get_convert_jobs(),
        max_age_hours=max_age_hours,
    )
    return result.to_dict()


@router.get("/maintenance/storage")
def get_storage_inventory_and_status():
    """Retrieve disk capacity, active category item counts, and retention policy settings."""
    retention_manager = get_retention_manager()
    inventory = retention_manager.get_storage_inventory()
    inventory["retention_policy"] = {
        "ttl_hours": retention_manager.config.ttl_hours,
        "cleanup_interval_minutes": retention_manager.config.cleanup_interval_minutes,
        "enabled": retention_manager.config.enabled,
        "max_disk_usage_percent": retention_manager.config.max_disk_usage_percent,
    }
    return inventory
