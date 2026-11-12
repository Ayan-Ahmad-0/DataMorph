"""Local disk storage adapter for job data files and intermediate artifacts with multi-tenant partitioning."""

from __future__ import annotations
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional


def write_json_atomic(path: Path, payload: Any) -> None:
    """Atomically write JSON data via a sibling temp file and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            json.dump(payload, tmp, ensure_ascii=False, indent=2, default=str)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def read_json_safe(path: Path) -> Optional[Any]:
    """Read and parse a JSON file, returning None if missing or unreadable."""
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


class JobStorage:
    """Manages workspace storage directories for each job with optional tenant partitioning."""

    def __init__(self, base_dir: str = "data/jobs", user_id: str = "default") -> None:
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.user_id = user_id

    def for_user(self, user_id: str) -> JobStorage:
        """Create a JobStorage adapter scoped to a specific tenant/user."""
        return JobStorage(base_dir=str(self.base_dir), user_id=user_id)

    def job_dir(self, job_id: str, user_id: Optional[str] = None) -> Path:
        """Return the physical job directory, partitioning by user_id.
        
        Gracefully resolves legacy unpartitioned directories if uid is 'default'.
        """
        uid = user_id or self.user_id
        if uid.lower() in ("anonymous", "none"):
            uid = "default"

        # Check if legacy unpartitioned directory exists for default tenant
        if uid == "default" and (self.base_dir / job_id).exists() and not (self.base_dir / uid / job_id).exists():
            return self.base_dir / job_id

        tenant_dir = self.base_dir / uid / job_id
        return tenant_dir

    def raw_file_path(self, job_id: str, filename: str, user_id: Optional[str] = None) -> Path:
        p = self.job_dir(job_id, user_id=user_id) / "raw"
        p.mkdir(parents=True, exist_ok=True)
        return p / filename

    def raw_csv_path(self, job_id: str, filename: str = "input.csv", user_id: Optional[str] = None) -> Path:
        return self.raw_file_path(job_id, filename, user_id=user_id)

    def staged_parquet_path(self, job_id: str, user_id: Optional[str] = None) -> Path:
        p = self.job_dir(job_id, user_id=user_id) / "parquet"
        p.mkdir(parents=True, exist_ok=True)
        return p / "staged.parquet"

    def clean_parquet_path(self, job_id: str, user_id: Optional[str] = None) -> Path:
        p = self.job_dir(job_id, user_id=user_id) / "clean"
        p.mkdir(parents=True, exist_ok=True)
        return p / "cleaned.parquet"

    def clean_csv_path(self, job_id: str, user_id: Optional[str] = None) -> Path:
        p = self.job_dir(job_id, user_id=user_id) / "clean"
        p.mkdir(parents=True, exist_ok=True)
        return p / "cleaned.csv"

    def clean_export_path(self, job_id: str, filename: str, user_id: Optional[str] = None) -> Path:
        p = self.job_dir(job_id, user_id=user_id) / "exports"
        p.mkdir(parents=True, exist_ok=True)
        return p / filename

    def verification_json_path(self, job_id: str, user_id: Optional[str] = None) -> Path:
        p = self.job_dir(job_id, user_id=user_id) / "reports"
        p.mkdir(parents=True, exist_ok=True)
        return p / "verification.json"

    def state_json_path(self, job_id: str, user_id: Optional[str] = None) -> Path:
        """Return the persistent job state snapshot path (does not create directories)."""
        return self.job_dir(job_id, user_id=user_id) / "state.json"

    def find_state_json_path(self, job_id: str, user_id: Optional[str] = None) -> Optional[Path]:
        """Locate an existing state.json for a job, scoping to user_id when given."""
        if user_id is not None:
            candidate = self.state_json_path(job_id, user_id=user_id)
            return candidate if candidate.exists() else None

        # Internal lookup without explicit user_id: check direct default & tenant folders
        direct_candidate = self.base_dir / job_id / "state.json"
        if direct_candidate.exists():
            return direct_candidate
        if self.base_dir.exists():
            for tenant_dir in self.base_dir.iterdir():
                if tenant_dir.is_dir():
                    candidate = tenant_dir / job_id / "state.json"
                    if candidate.exists():
                        return candidate
        return None

    def cleanup_job(self, job_id: str, user_id: Optional[str] = None) -> None:
        """Remove a job workspace directory."""
        uid = user_id or self.user_id
        target = self.base_dir / uid / job_id
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        elif (self.base_dir / job_id).exists() and uid == "default":
            shutil.rmtree(self.base_dir / job_id, ignore_errors=True)

        # Clean empty tenant parent directory if no other jobs remain
        user_parent = self.base_dir / uid
        if user_parent.exists() and user_parent != self.base_dir and not any(user_parent.iterdir()):
            shutil.rmtree(user_parent, ignore_errors=True)
