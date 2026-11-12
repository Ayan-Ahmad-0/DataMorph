"""Shared FastAPI dependencies and file streaming utilities resolving app.main singletons."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from app.orchestrator import JobOrchestrator
    from app.storage import JobStorage
    from core.modeling.service import ModelingService
    from core.retention import StorageRetentionManager


def get_storage() -> "JobStorage":
    """Resolve active JobStorage instance (supports runtime monkeypatching in tests)."""
    import app.main

    return app.main.storage


def get_orchestrator_dep() -> "JobOrchestrator":
    """Resolve active JobOrchestrator instance."""
    import app.main

    return app.main.orchestrator


def get_retention_manager() -> "StorageRetentionManager":
    """Resolve active StorageRetentionManager instance."""
    import app.main

    return app.main.retention_manager


def get_convert_jobs() -> Dict[str, Any]:
    """Resolve shared convert/query job state dictionary."""
    import app.main

    return app.main.convert_jobs


def get_modeling_service() -> "ModelingService":
    """Resolve active ModelingService instance."""
    import app.main

    return app.main.modeling_service


def copy_stream_to_path(src_file, dest_path: Path) -> int:
    """Copy an uploaded file stream to disk using 1 MB chunks and return byte count."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dest_path, "wb") as buffer:
        shutil.copyfileobj(src_file, buffer, length=1024 * 1024)
    return dest_path.stat().st_size
