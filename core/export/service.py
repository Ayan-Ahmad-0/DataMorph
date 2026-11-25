"""High-level orchestration service for recipe-to-code export and ZIP packaging."""

from __future__ import annotations

import io
import zipfile
from typing import Any, Dict, List, Optional

from core.export.base import Emitter
from core.export.dbt_emitter import DbtEmitter
from core.export.ir import compile_recipe_to_ir
from core.export.models import (
    ExportCodeRequest,
    ExportCodeResponse,
    ExportOptions,
    ExportTarget,
)
from core.export.pandas_emitter import PandasEmitter
from core.export.polars_emitter import PolarsEmitter
from core.export.sql_emitter import SqlEmitter
from core.recipe_schema import Recipe

_EMITTERS: Dict[ExportTarget, Emitter] = {
    "sql": SqlEmitter(),
    "dbt": DbtEmitter(),
    "pandas": PandasEmitter(),
    "polars": PolarsEmitter(),
}


def get_emitter(target: ExportTarget) -> Emitter:
    """Resolve the emitter implementation for the requested export target."""
    emitter = _EMITTERS.get(target)
    if emitter is None:
        raise ValueError(f"Unsupported export target: '{target}'")
    return emitter


def export_recipe_as_code(
    recipe: Recipe | Dict[str, Any],
    request: ExportCodeRequest,
    *,
    job_id: str,
    dataset_name: Optional[str] = None,
    profile: Optional[Dict[str, Any]] = None,
    extra_warnings: Optional[List[str]] = None,
) -> ExportCodeResponse:
    """Validate/compile a recipe to IR and emit target code files with parity warnings."""
    ir = compile_recipe_to_ir(recipe, profile=profile)
    emitter = get_emitter(request.target)

    options = ExportOptions(
        job_id=job_id,
        dialect=request.dialect,
        include_tests=request.include_tests,
        source_name=request.source_name,
        dataset_name=dataset_name,
    )
    emitted = emitter.emit(ir, options)

    warnings: List[str] = []
    seen: set[str] = set()
    for msg in (extra_warnings or []) + emitted.warnings:
        if msg and msg not in seen:
            seen.add(msg)
            warnings.append(msg)

    response_dialect: Optional[str] = request.dialect if request.target in ("sql", "dbt") else None
    return ExportCodeResponse(
        target=request.target,
        dialect=response_dialect,
        files=emitted.files,
        warnings=warnings,
    )


def package_export_zip(response: ExportCodeResponse) -> bytes:
    """Package all exported files into an in-memory ZIP archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for exported_file in response.files:
            info = zipfile.ZipInfo(exported_file.name, date_time=(2025, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, exported_file.content.encode("utf-8"))
    return buf.getvalue()
