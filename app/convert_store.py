"""Disk persistence helpers for convert and query job metadata."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from app.storage import read_json_safe, write_json_atomic
from formats.convert import ConvertResult

logger = logging.getLogger(__name__)


def _normalize_uid(user_id: str) -> str:
    if not user_id or user_id.lower() in ("anonymous", "none"):
        return "default"
    return user_id


def save_convert_job_meta(job_data: Dict[str, Any]) -> None:
    """Persist convert or query job metadata to meta.json inside its work_dir."""
    work_dir = Path(job_data["work_dir"])
    ttl_hours = float(os.getenv("STORAGE_TTL_HOURS", "24"))
    expires_at = job_data.get("expires_at")
    if isinstance(expires_at, datetime):
        expires_iso = expires_at.isoformat()
    elif isinstance(expires_at, str) and expires_at:
        expires_iso = expires_at
    else:
        expires_iso = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()

    result_obj = job_data.get("result")
    result_payload: Optional[Dict[str, Any]] = None
    if isinstance(result_obj, ConvertResult):
        result_payload = {
            "output_files": [str(p) for p in result_obj.output_files],
            "source_format": result_obj.source_format,
            "target_format": result_obj.target_format,
            "rows": result_obj.rows,
            "columns": list(result_obj.columns),
            "warnings": list(result_obj.warnings),
            "intermediate_parquet": str(result_obj.intermediate_parquet) if result_obj.intermediate_parquet else None,
            "elapsed_seconds": result_obj.elapsed_seconds,
        }
    elif isinstance(result_obj, dict):
        result_payload = result_obj

    inter_pq = job_data.get("intermediate_parquet")
    payload = {
        "schema_version": 1,
        "id": job_data["id"],
        "user_id": _normalize_uid(str(job_data.get("user_id", "default"))),
        "filename": job_data["filename"],
        "work_dir": str(work_dir),
        "intermediate_parquet": str(inter_pq) if inter_pq else None,
        "expires_at": expires_iso,
        "result": result_payload,
    }
    try:
        write_json_atomic(work_dir / "meta.json", payload)
    except Exception as err:
        logger.warning("Failed to persist convert/query meta.json for '%s': %s", job_data.get("id"), err)


def _find_meta_path(job_id: str, user_id: str, base_data_dir: Path) -> Optional[Path]:
    uid = _normalize_uid(user_id)
    for category in ("convert_jobs", "query_jobs"):
        candidate = base_data_dir / category / uid / job_id / "meta.json"
        if candidate.exists():
            return candidate
        if uid == "default":
            legacy_candidate = base_data_dir / category / job_id / "meta.json"
            if legacy_candidate.exists():
                return legacy_candidate
    return None


def get_convert_job(
    convert_jobs: Dict[str, Any],
    convert_id: str,
    user_id: str,
    base_data_dir: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch convert/query job from RAM or rehydrate from meta.json on disk."""
    req_uid = _normalize_uid(user_id)
    in_mem = convert_jobs.get(convert_id)
    if in_mem is not None:
        if _normalize_uid(str(in_mem.get("user_id", "default"))) != req_uid:
            return None
        return in_mem

    data_root = (base_data_dir or Path("data")).resolve()
    meta_path = _find_meta_path(convert_id, req_uid, data_root)
    if not meta_path:
        return None

    raw = read_json_safe(meta_path)
    if not isinstance(raw, dict):
        return None

    owner_uid = _normalize_uid(str(raw.get("user_id", "default")))
    if owner_uid != req_uid:
        return None

    exp_val = raw.get("expires_at")
    if isinstance(exp_val, str) and exp_val:
        try:
            exp_dt = datetime.fromisoformat(exp_val)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if exp_dt < datetime.now(timezone.utc):
                return None
        except Exception:
            pass

    work_dir = Path(raw["work_dir"]) if raw.get("work_dir") else meta_path.parent
    inter_pq = Path(raw["intermediate_parquet"]) if raw.get("intermediate_parquet") else None

    result_obj: Optional[ConvertResult] = None
    raw_res = raw.get("result")
    if isinstance(raw_res, dict):
        result_obj = ConvertResult(
            output_files=[Path(p) for p in raw_res.get("output_files", [])],
            source_format=str(raw_res.get("source_format", "")),
            target_format=str(raw_res.get("target_format", "")),
            rows=int(raw_res.get("rows", 0)),
            columns=list(raw_res.get("columns", [])),
            warnings=list(raw_res.get("warnings", [])),
            intermediate_parquet=Path(raw_res["intermediate_parquet"]) if raw_res.get("intermediate_parquet") else None,
            elapsed_seconds=float(raw_res.get("elapsed_seconds", 0.0)),
        )

    rehydrated: Dict[str, Any] = {
        "id": raw.get("id", convert_id),
        "user_id": owner_uid,
        "filename": raw.get("filename", ""),
        "work_dir": work_dir,
        "intermediate_parquet": inter_pq,
        "expires_at": exp_val,
    }
    if result_obj is not None:
        rehydrated["result"] = result_obj

    convert_jobs[convert_id] = rehydrated
    return rehydrated

