"""Shared FastAPI dependencies and file streaming utilities resolving app.main singletons."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from fastapi import HTTPException, status

if TYPE_CHECKING:
    from app.orchestrator import JobOrchestrator
    from app.storage import JobStorage
    from core.modeling.service import ModelingService
    from core.retention import StorageRetentionManager


class UploadTooLargeError(HTTPException):
    """Raised when a streamed upload exceeds the configured byte limit."""

    def __init__(self, max_bytes: int) -> None:
        max_mb = max_bytes / (1024 * 1024)
        super().__init__(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Uploaded file exceeds maximum allowed size ({max_mb:.0f} MB).",
        )


def get_max_upload_bytes() -> int:
    """Return configured upload size ceiling in bytes (default 5120 MB / 5 GB)."""
    try:
        max_mb = int(os.getenv("MAX_UPLOAD_MB", "5120"))
    except ValueError:
        max_mb = 5120
    return max(1, max_mb) * 1024 * 1024


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


def copy_stream_to_path(src_file, dest_path: Path, max_bytes: Optional[int] = None) -> int:
    """Copy an uploaded file stream to disk using 1 MB chunks while enforcing an optional byte cap."""
    limit = get_max_upload_bytes() if max_bytes is None else max_bytes
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    try:
        with open(dest_path, "wb") as buffer:
            while True:
                chunk = src_file.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if limit > 0 and total_bytes > limit:
                    raise UploadTooLargeError(limit)
                buffer.write(chunk)
    except Exception:
        if dest_path.exists():
            dest_path.unlink(missing_ok=True)
        raise
    return total_bytes

