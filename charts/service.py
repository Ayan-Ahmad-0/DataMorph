"""Public service API for charts (Section 5 of spec)."""

from __future__ import annotations
from collections import OrderedDict
import csv
from dataclasses import dataclass, field
import hashlib
import json
import logging
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.storage import JobStorage
from charts.compile import compile_spec, quote_identifier
from charts.errors import ChartError, ChartTimeoutError, ChartTooLargeError, DataNotFoundError, InvalidChartSpecError, ServerBusyError
from charts.limits import ChartLimits
from charts.shape import ChartSuggestion, chart_from_result
from charts.spec import ChartSpec, validate_spec
from charts.stats import ColumnStats, compute_column_stats
from charts.suggest import suggest_charts_from_stats
from core.duckdb_utils import (
    configure_duckdb_connection,
    get_chart_concurrency_limits,
    get_duckdb_memory_limit,
)
from explore.limits import ConcurrencyController, QueryLimits
from explore.sandbox import create_sandboxed_connection
from explore.service import concurrency_guard, resolve_job_parquet_paths, serialize_cell_value

logger = logging.getLogger(__name__)

_global_chart_conc, _per_user_chart_conc = get_chart_concurrency_limits()
_chart_mem_limit = get_duckdb_memory_limit()

# Dedicated concurrency controller for dashboard chart execution scaled to host RAM
_chart_limits = QueryLimits(
    query_timeout_seconds=60.0,
    memory_limit_per_query=_chart_mem_limit,
    max_concurrent_queries_global=_global_chart_conc,
    max_concurrent_queries_per_user=_per_user_chart_conc,
)
_chart_controller = ConcurrencyController(_chart_limits)


@dataclass
class ChartData:
    spec: ChartSpec
    columns: List[str]
    rows: List[List[Any]]
    row_count: int
    notes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "spec": self.spec.model_dump(),
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "notes": self.notes,
            "warnings": self.warnings,
            "elapsed_seconds": self.elapsed_seconds,
        }


# Bounded LRU + TTL caches partitioned by tenant
MAX_STATS_CACHE_ENTRIES = 128
MAX_CHART_CACHE_ENTRIES = 256
_cache_lock = threading.Lock()
_stats_cache: OrderedDict[Tuple[str, str], Tuple[float, List[ColumnStats]]] = OrderedDict()
_chart_cache: OrderedDict[str, Tuple[float, ChartData]] = OrderedDict()


def _cache_get(cache: OrderedDict, key: Any, ttl_seconds: float) -> Optional[Any]:
    now = time.time()
    with _cache_lock:
        entry = cache.get(key)
        if not entry:
            return None
        ts, val = entry
        if now - ts >= ttl_seconds:
            cache.pop(key, None)
            return None
        cache.move_to_end(key)
        return val


def _cache_set(cache: OrderedDict, key: Any, value: Any, max_entries: int) -> None:
    now = time.time()
    with _cache_lock:
        cache[key] = (now, value)
        cache.move_to_end(key)
        while len(cache) > max_entries:
            cache.popitem(last=False)


def _get_spec_hash(job_id: str, spec: ChartSpec, user_id: str = "default") -> str:
    serialized = spec.model_dump_json(exclude_none=True)
    return hashlib.sha256(f"{user_id}:{job_id}:{serialized}".encode("utf-8")).hexdigest()


def get_column_stats(
    job_id: str,
    user_id: str = "anonymous",
    storage: Optional[JobStorage] = None,
    limits: Optional[ChartLimits] = None,
) -> List[ColumnStats]:
    """Retrieve or compute column stats for a dataset."""
    lim = limits or ChartLimits()
    uid = user_id or "default"
    cache_key = (uid, job_id)

    cached_stats = _cache_get(_stats_cache, cache_key, lim.chart_cache_ttl_seconds)
    if cached_stats is not None:
        return cached_stats

    clean_pq, orig_pq = resolve_job_parquet_paths(job_id, user_id=uid, storage=storage)
    # Prefer cleaned Parquet if available (e.g. post-cleaning), else staged/raw Parquet
    target_pq = clean_pq if clean_pq and clean_pq.exists() else (orig_pq or clean_pq)

    stats = compute_column_stats(target_pq, limits=lim)
    _cache_set(_stats_cache, cache_key, stats, MAX_STATS_CACHE_ENTRIES)
    return stats


def suggest_charts(
    job_id: str,
    user_id: str = "anonymous",
    max_suggestions: int = 8,
    storage: Optional[JobStorage] = None,
    limits: Optional[ChartLimits] = None,
) -> List[ChartSpec]:
    """Suggest charts deterministically based on dataset statistics."""
    stats = get_column_stats(job_id, user_id=user_id, storage=storage, limits=limits)
    
    clean_pq, orig_pq = resolve_job_parquet_paths(job_id, user_id=user_id, storage=storage)
    target_pq = clean_pq if clean_pq and clean_pq.exists() else (orig_pq or clean_pq)

    # Quick row count
    import duckdb
    con = duckdb.connect(":memory:")
    try:
        row_count = con.execute(f"SELECT COUNT(*) FROM read_parquet('{target_pq.as_posix()}')").fetchone()[0]
    finally:
        con.close()

    return suggest_charts_from_stats(stats, row_count=row_count, max_suggestions=max_suggestions)


from charts.page_options import PageDataOptions, apply_page_options

def build_chart(
    job_id: str,
    user_id: str = "anonymous",
    spec: Optional[ChartSpec] = None,
    page_options: Optional[PageDataOptions] = None,
    storage: Optional[JobStorage] = None,
    limits: Optional[ChartLimits] = None,
) -> ChartData:
    """Compile and execute a ChartSpec on the sandboxed DuckDB engine."""
    if spec is None:
        raise InvalidChartSpecError("No ChartSpec provided.")

    lim = limits or ChartLimits()
    if spec.chart_type == "pie":
        if spec.top_n and spec.top_n > lim.max_pie_slices:
            spec.top_n = lim.max_pie_slices
        elif not spec.top_n:
            spec.top_n = lim.max_pie_slices
        if spec.aggregation == "none":
            spec.aggregation = "count"

    # Resolve dataset paths
    clean_pq, orig_pq = resolve_job_parquet_paths(job_id, user_id=user_id, storage=storage)
    target_pq = clean_pq if clean_pq and clean_pq.exists() else (orig_pq or clean_pq)

    # 1. Fetch schema
    import duckdb
    con_meta = duckdb.connect(":memory:")
    try:
        desc = con_meta.execute(f"DESCRIBE SELECT * FROM read_parquet('{target_pq.as_posix()}')").fetchall()
        schema = {str(row[0]): str(row[1]) for row in desc}
    finally:
        con_meta.close()

    # Auto-adapt / heal spec for target chart type to prevent user errors when switching types
    from charts.spec import FieldRef, _is_numeric_type
    ctype = spec.chart_type
    if ctype in ("pie", "donut"):
        if spec.top_n and spec.top_n > lim.max_pie_slices:
            spec.top_n = lim.max_pie_slices
        elif not spec.top_n:
            spec.top_n = lim.max_pie_slices
        if spec.aggregation == "none":
            spec.aggregation = "count"
        if not spec.x and schema:
            spec.x = FieldRef(column=list(schema.keys())[0])
    elif ctype == "box":
        if not spec.y or not _is_numeric_type(schema.get(spec.y.column, ""), spec.y.cast_as):
            if spec.x and _is_numeric_type(schema.get(spec.x.column, ""), spec.x.cast_as):
                spec.y = spec.x
                spec.x = None
            else:
                for col_name, col_type in schema.items():
                    if _is_numeric_type(col_type, None):
                        spec.y = FieldRef(column=col_name)
                        break
        if spec.x and spec.y and spec.x.column == spec.y.column:
            spec.x = None
    elif ctype == "heatmap":
        cols = list(schema.keys())
        if not spec.x and cols:
            spec.x = FieldRef(column=cols[0])
        if not spec.y or (spec.x and spec.y.column == spec.x.column):
            for c in cols:
                if not spec.x or c != spec.x.column:
                    spec.y = FieldRef(column=c)
                    break
            if not spec.y and cols:
                spec.y = FieldRef(column=cols[0])
        spec.aggregation = "count"
    elif ctype == "histogram":
        if not spec.x or not _is_numeric_type(schema.get(spec.x.column, ""), spec.x.cast_as):
            for col_name, col_type in schema.items():
                if _is_numeric_type(col_type, None):
                    spec.x = FieldRef(column=col_name)
                    break
        spec.y = None
    elif ctype == "scatter":
        num_cols = [c for c, t in schema.items() if _is_numeric_type(t, None)]
        if not spec.x or not _is_numeric_type(schema.get(spec.x.column, ""), spec.x.cast_as):
            if num_cols:
                spec.x = FieldRef(column=num_cols[0])
        if not spec.y or not _is_numeric_type(schema.get(spec.y.column, ""), spec.y.cast_as):
            if len(num_cols) > 1:
                spec.y = FieldRef(column=num_cols[1])
            elif num_cols:
                spec.y = FieldRef(column=num_cols[0])
        spec.aggregation = "none"
    elif ctype == "big_number":
        spec.x = None
        if spec.aggregation == "none":
            spec.aggregation = "count"
        if spec.y and not _is_numeric_type(schema.get(spec.y.column, ""), spec.y.cast_as):
            spec.y.cast_as = "number"
    elif ctype in ("bar", "hbar", "line", "area"):
        if not spec.x and schema:
            spec.x = FieldRef(column=list(schema.keys())[0])

    # For any chart type with numeric aggregation, ensure non-numeric Y columns are cast as number
    if spec.y and not spec.y.formula and spec.aggregation in ("sum", "avg", "median"):
        y_col_type = schema.get(spec.y.column, "")
        if not _is_numeric_type(y_col_type, spec.y.cast_as):
            spec.y.cast_as = "number"

    # Apply page options if provided
    if page_options:
        spec = apply_page_options(spec, page_options, schema)

    cache_key = _get_spec_hash(job_id, spec, user_id=user_id)
    cached_chart = _cache_get(_chart_cache, cache_key, lim.chart_cache_ttl_seconds)
    if cached_chart is not None:
        return cached_chart

    # 2. Compile to SQL
    sql, params, notes = compile_spec(spec, schema, limits=lim)
    warnings: List[str] = []

    if page_options:
        if page_options.exclude_missing:
            notes.append("Exclude missing values: on")
        if page_options.hide_outliers:
            notes.append("Hide outliers: on (1st-99th percentile)")
        for col, cast_t in page_options.cast_overrides.items():
            if (spec.x and spec.x.column == col) or (spec.y and spec.y.column == col):
                notes.append(f"'{col}' read as {cast_t}")

    # 3. Check for cast failure warnings in a single batched Parquet scan
    cast_fields = [f for f in (spec.x, spec.y) if f and not f.formula and f.cast_as in ("number", "date", "timestamp")]
    if cast_fields:
        cast_con = duckdb.connect(":memory:")
        configure_duckdb_connection(cast_con)
        try:
            select_exprs = []
            for idx, f in enumerate(cast_fields):
                col_q = quote_identifier(f.column)
                cast_type = "DOUBLE" if f.cast_as == "number" else ("DATE" if f.cast_as == "date" else "TIMESTAMP")
                select_exprs.append(f"COUNT({col_q}) AS nn_{idx}")
                select_exprs.append(
                    f"SUM(CASE WHEN {col_q} IS NOT NULL AND TRY_CAST({col_q} AS {cast_type}) IS NULL THEN 1 ELSE 0 END) AS fc_{idx}"
                )
            chk_sql = f"SELECT {', '.join(select_exprs)} FROM read_parquet('{target_pq.as_posix()}')"
            res = cast_con.execute(chk_sql).fetchone()
            if res:
                for idx, f in enumerate(cast_fields):
                    total_nn = res[idx * 2] or 0
                    failed_c = res[idx * 2 + 1] or 0
                    if total_nn > 0 and failed_c > 0:
                        pct = round((failed_c / total_nn) * 100.0, 1)
                        warnings.append(
                            f"{pct}% of '{f.column}' values could not be read as {f.cast_as} and were ignored."
                        )
        except Exception as e:
            logger.warning("Failed to calculate cast warnings: %s", e)
        finally:
            cast_con.close()

    # 4. Execute query with RAM-tuned limits & concurrency
    q_lim = QueryLimits(
        query_timeout_seconds=lim.chart_query_timeout_seconds,
        memory_limit_per_query=get_duckdb_memory_limit(),
    )

    t0 = time.perf_counter()
    with concurrency_guard(user_id, controller=_chart_controller, timeout=15.0):
        con = create_sandboxed_connection(target_pq, limits=q_lim)
        try:
            rel = con.execute(sql, params)
            col_names = [str(d[0]) for d in rel.description]
            raw_rows = rel.fetchall()
        except (duckdb.InterruptException, duckdb.OutOfMemoryException) as e:
            if isinstance(e, duckdb.InterruptException):
                raise ChartTimeoutError(lim.chart_query_timeout_seconds)
            raise ChartTooLargeError(f"Query memory limit exceeded: {e}")
        except Exception as e:
            err_msg = str(e).lower()
            if "timeout" in err_msg or "interrupted" in err_msg:
                raise ChartTimeoutError(lim.chart_query_timeout_seconds)
            logger.exception("Error executing chart query for job %s: %s", job_id, e)
            raise ChartError(f"Chart query execution failed: {e}")
        finally:
            con.close()

    elapsed = round(time.perf_counter() - t0, 3)

    # Serialize rows
    serialized_rows: List[List[Any]] = []
    for r in raw_rows:
        row_vals = []
        for val in r:
            s_val, _ = serialize_cell_value(val)
            row_vals.append(s_val)
        serialized_rows.append(row_vals)

    chart_data = ChartData(
        spec=spec,
        columns=col_names,
        rows=serialized_rows,
        row_count=len(serialized_rows),
        notes=notes,
        warnings=warnings,
        elapsed_seconds=elapsed,
    )

    _cache_set(_chart_cache, cache_key, chart_data, MAX_CHART_CACHE_ENTRIES)
    return chart_data


def export_chart_data(
    job_id: str,
    user_id: str = "anonymous",
    spec: Optional[ChartSpec] = None,
    out_dir: Optional[Path] = None,
    storage: Optional[JobStorage] = None,
    limits: Optional[ChartLimits] = None,
) -> Path:
    """Execute chart query and export aggregated rows to CSV."""
    data = build_chart(job_id, user_id=user_id, spec=spec, storage=storage, limits=limits)
    
    target_dir = out_dir or (Path("data/exports").resolve() / job_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    out_file = target_dir / f"chart_data_{int(time.time())}.csv"

    with open(out_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(data.columns)
        for row in data.rows:
            writer.writerow(row)

    return out_file


def validate_formula(
    job_id: str,
    formula: str,
    user_id: str = "anonymous",
    storage: Optional[JobStorage] = None,
) -> Dict[str, Any]:
    """Validate a custom formula expression against the dataset schema in DuckDB.
    
    Returns {valid: bool, inferred_type: Optional[str], sample_result: Any, error: Optional[str]}.
    """
    from charts.spec import validate_formula_string
    try:
        validate_formula_string(formula)
    except Exception as e:
        return {"valid": False, "inferred_type": None, "sample_result": None, "error": str(e)}

    clean_pq, orig_pq = resolve_job_parquet_paths(job_id, user_id=user_id, storage=storage)
    target_pq = clean_pq if clean_pq and clean_pq.exists() else (orig_pq or clean_pq)
    if not target_pq or not target_pq.exists():
        return {"valid": False, "inferred_type": None, "sample_result": None, "error": "Dataset not found."}

    q_lim = QueryLimits(query_timeout_seconds=5.0, memory_limit_per_query="1GB")
    try:
        con = create_sandboxed_connection(target_pq, limits=q_lim)
        try:
            rel = con.execute(f"SELECT ({formula.strip()}) AS calc_val FROM data LIMIT 1")
            desc = rel.description
            inferred_type = str(desc[0][1]) if desc else "UNKNOWN"
            row = rel.fetchone()
            sample_val = row[0] if row else None
            if sample_val is not None and hasattr(sample_val, "isoformat"):
                sample_val = sample_val.isoformat()
            return {
                "valid": True,
                "inferred_type": inferred_type,
                "sample_result": sample_val,
                "error": None,
            }
        finally:
            con.close()
    except Exception as e:
        err_msg = str(e)
        if "Binder Error:" in err_msg:
            err_msg = err_msg.split("Binder Error:")[-1].strip()
        elif "Parser Error:" in err_msg:
            err_msg = err_msg.split("Parser Error:")[-1].strip()
        return {
            "valid": False,
            "inferred_type": None,
            "sample_result": None,
            "error": err_msg,
        }
