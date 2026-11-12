"""Single-pass DuckDB profiler generating structured profile.json."""

from __future__ import annotations
import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
import duckdb

from core.duckdb_utils import (
    configure_duckdb_connection,
    get_duckdb_memory_limit,
    get_duckdb_temp_directory,
    get_duckdb_threads,
)

logger = logging.getLogger(__name__)

PROFILE_BATCH_SIZE = 15

NULL_TOKENS = ["", "n/a", "na", "null", "none", "-", "?", "undefined", "nan"]

DISPLAY_TOKEN_MAP = {
    "": "<empty>",
    "n/a": "N/A",
    "na": "NA",
    "null": "null",
    "none": "none",
    "-": "-",
    "?": "?",
    "undefined": "undefined",
    "nan": "NaN",
}

COMMON_DATE_FORMATS = [
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%Y/%m/%d",
    "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S%z",
]


@dataclass
class DateFormatMatch:
    format: str
    match_pct: float


@dataclass
class NumericStats:
    min: Optional[float] = None
    max: Optional[float] = None
    median: Optional[float] = None
    q25: Optional[float] = None
    q75: Optional[float] = None
    negatives: int = 0
    negative_pct: float = 0.0


@dataclass
class ColumnProfile:
    name: str
    inferred_type: str
    total_count: int
    nulls: int
    null_pct: float
    null_tokens: Dict[str, int] = field(default_factory=dict)
    distinct_count: int = 0
    candidate_key: bool = False
    whitespace_issues: int = 0
    casing_issues: int = 0
    numeric_stats: Optional[NumericStats] = None
    formats: List[DateFormatMatch] = field(default_factory=list)
    timezone_evidence: Optional[str] = None
    day_month_ambiguous: bool = False
    sample_values: List[str] = field(default_factory=list)


@dataclass
class DuplicateSummary:
    exact_rows: int
    exact_pct: float


@dataclass
class DatasetProfile:
    file_info: Dict[str, Any]
    columns: Dict[str, ColumnProfile]
    duplicates: DuplicateSummary
    duration_seconds: float

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dictionary matching spec format."""
        cols_dict = {}
        for col_name, cp in self.columns.items():
            entry: Dict[str, Any] = {
                "inferred_type": cp.inferred_type,
                "nulls": cp.nulls,
                "null_pct": cp.null_pct,
                "distinct": cp.distinct_count,
                "candidate_key": cp.candidate_key,
            }
            if cp.null_tokens:
                entry["null_tokens"] = cp.null_tokens
            if cp.whitespace_issues > 0:
                entry["whitespace_issues"] = cp.whitespace_issues
            if cp.casing_issues > 0:
                entry["casing_issues"] = cp.casing_issues
            if cp.numeric_stats:
                entry["min"] = cp.numeric_stats.min
                entry["max"] = cp.numeric_stats.max
                entry["median"] = cp.numeric_stats.median
                entry["negatives"] = cp.numeric_stats.negatives
                entry["negative_pct"] = cp.numeric_stats.negative_pct
            if cp.formats:
                entry["formats"] = [asdict(f) for f in cp.formats]
            if cp.timezone_evidence:
                entry["timezone_evidence"] = cp.timezone_evidence
            if cp.day_month_ambiguous:
                entry["day_month_ambiguous"] = True
            if cp.sample_values:
                entry["sample_values"] = cp.sample_values

            cols_dict[col_name] = entry

        return {
            "file": self.file_info,
            "columns": cols_dict,
            "duplicates": asdict(self.duplicates),
            "profile_duration_seconds": self.duration_seconds,
        }


class Profiler:
    """High-throughput single-pass dataset profiler using DuckDB."""

    def __init__(
        self,
        memory_limit: Optional[str] = None,
        threads: Optional[int] = None,
        privacy_mode: bool = False,
        temp_directory: Optional[str] = None,
    ) -> None:
        self.memory_limit = memory_limit or get_duckdb_memory_limit()
        self.threads = threads or get_duckdb_threads()
        self.privacy_mode = privacy_mode
        self.temp_directory = temp_directory or get_duckdb_temp_directory()

    def _get_connection(self) -> duckdb.DuckDBPyConnection:
        conn = duckdb.connect(":memory:")
        return configure_duckdb_connection(
            conn,
            memory_limit=self.memory_limit,
            threads=self.threads,
            temp_directory=self.temp_directory,
        )

    def profile_parquet(
        self,
        parquet_path: str | Path,
        source_csv_path: Optional[str | Path] = None,
        progress_callback: Optional[Callable[[float, str], None]] = None,
    ) -> DatasetProfile:
        """Profile a text-only Parquet file produced by Converter."""
        start_time = time.perf_counter()
        pq_path = Path(parquet_path).resolve()
        if not pq_path.exists():
            raise FileNotFoundError(f"Parquet file not found: {pq_path}")

        pq_sql = str(pq_path).replace("\\", "/")
        conn = self._get_connection()

        try:
            if progress_callback:
                progress_callback(50.0, "Scanning table schema and duplicate records...")

            # Register parquet view
            view_name = "raw_data"
            conn.execute(f"CREATE OR REPLACE VIEW {view_name} AS SELECT * FROM read_parquet('{pq_sql}');")

            # 1. Total row count and columns
            desc = conn.execute(f"DESCRIBE {view_name}").fetchall()
            column_names = [col[0] for col in desc]
            col_count = len(column_names)

            total_rows = conn.execute(f"SELECT COUNT(*) FROM {view_name}").fetchone()[0]

            # 2. Duplicate detection (high-efficiency 64-bit row hash to prevent memory exhaustion)
            if total_rows > 0:
                cols_quoted = ", ".join(f'"{col}"' for col in column_names)
                if total_rows > 100000:
                    # Fast short-circuit check for large datasets: verify if any duplicate rows exist
                    # Grouping by 64-bit row hash prevents allocating memory for 15+ VARCHAR columns
                    has_dup = conn.execute(
                        f"SELECT 1 FROM (SELECT hash({cols_quoted}) AS _h FROM {view_name} GROUP BY _h HAVING COUNT(*) > 1 LIMIT 1)"
                    ).fetchone()
                    if has_dup is None:
                        exact_dup_rows = 0
                        exact_dup_pct = 0.0
                    else:
                        exact_dup_rows = conn.execute(
                            f"SELECT COUNT(*) - COUNT(DISTINCT hash({cols_quoted})) FROM {view_name}"
                        ).fetchone()[0]
                        exact_dup_pct = round((exact_dup_rows / total_rows) * 100, 2)
                else:
                    exact_dup_rows = conn.execute(
                        f"SELECT COUNT(*) - COUNT(DISTINCT hash({cols_quoted})) FROM {view_name}"
                    ).fetchone()[0]
                    exact_dup_pct = round((exact_dup_rows / total_rows) * 100, 2)
            else:
                exact_dup_rows = 0
                exact_dup_pct = 0.0

            if progress_callback:
                progress_callback(52.0, "Duplicate scan complete. Profiling columns...")

            # 3. Profile columns in multi-column batches to minimize full Parquet scans
            columns_profile: Dict[str, ColumnProfile] = {}
            if total_rows == 0:
                for col in column_names:
                    columns_profile[col] = ColumnProfile(
                        name=col,
                        inferred_type="string",
                        total_count=0,
                        nulls=0,
                        null_pct=0.0,
                    )
            else:
                for batch_start in range(0, col_count, PROFILE_BATCH_SIZE):
                    batch = column_names[batch_start : batch_start + PROFILE_BATCH_SIZE]
                    if progress_callback and col_count > 0:
                        pct = 52.0 + (batch_start / col_count) * 43.0
                        progress_callback(
                            pct,
                            f"Profiling columns {batch_start + 1}–{batch_start + len(batch)} of {col_count}...",
                        )
                    batch_profiles = self._profile_columns_batch(conn, view_name, batch, total_rows)
                    columns_profile.update(batch_profiles)

            if progress_callback:
                progress_callback(96.0, "Statistical profile ready. Compiling cleaning recipe...")

            elapsed = round(time.perf_counter() - start_time, 3)

            # File size info
            file_size_bytes = pq_path.stat().st_size
            if source_csv_path and Path(source_csv_path).exists():
                file_size_bytes = Path(source_csv_path).stat().st_size

            file_info = {
                "rows": total_rows,
                "columns": col_count,
                "size_bytes": file_size_bytes,
                "size_gb": round(file_size_bytes / (1024**3), 3),
            }

            return DatasetProfile(
                file_info=file_info,
                columns=columns_profile,
                duplicates=DuplicateSummary(
                    exact_rows=exact_dup_rows, exact_pct=exact_dup_pct
                ),
                duration_seconds=elapsed,
            )

        finally:
            conn.close()

    @staticmethod
    def _quote_ident(col_name: str) -> str:
        return f'"{col_name.replace(chr(34), chr(34) * 2)}"'

    def _profile_columns_batch(
        self,
        conn: duckdb.DuckDBPyConnection,
        view_name: str,
        batch_cols: List[str],
        total_rows: int,
    ) -> Dict[str, ColumnProfile]:
        """Compute Phase 1 counts, Phase 2 type inference, and numeric stats in batched DuckDB scans."""
        null_token_sql = ",".join(f"'{t}'" for t in NULL_TOKENS)
        num_tokens = len(NULL_TOKENS)
        stride_p1 = 3 + num_tokens

        # Phase 1: Single full scan across all columns in the batch
        p1_exprs: List[str] = []
        for idx, col_name in enumerate(batch_cols):
            quoted = self._quote_ident(col_name)
            str_val = f"try_cast({quoted} AS VARCHAR)"
            distinct_expr = (
                f"COUNT(DISTINCT {quoted})"
                if total_rows <= 100000
                else f"APPROX_COUNT_DISTINCT({quoted})"
            )
            p1_exprs.append(f"COUNT({quoted}) AS c{idx}_nn")
            p1_exprs.append(f"{distinct_expr} AS c{idx}_dist")
            p1_exprs.append(
                f"SUM(CASE WHEN {quoted} IS NOT NULL AND length({str_val}) != length(trim({str_val})) THEN 1 ELSE 0 END) AS c{idx}_ws"
            )
            for t_idx, tok in enumerate(NULL_TOKENS):
                p1_exprs.append(
                    f"SUM(CASE WHEN lower(trim({str_val})) = '{tok}' THEN 1 ELSE 0 END) AS c{idx}_tok{t_idx}"
                )

        p1_row = conn.execute(f"SELECT {', '.join(p1_exprs)} FROM {view_name};").fetchone()

        phase1_meta: Dict[str, Dict[str, Any]] = {}
        for idx, col_name in enumerate(batch_cols):
            quoted = self._quote_ident(col_name)
            base = idx * stride_p1
            non_null_count = p1_row[base] or 0
            distinct_count = p1_row[base + 1] or 0
            ws_issues = p1_row[base + 2] or 0

            token_counts: Dict[str, int] = {}
            for t_idx, tok in enumerate(NULL_TOKENS):
                val = p1_row[base + 3 + t_idx] or 0
                if val > 0:
                    display_tok = DISPLAY_TOKEN_MAP.get(tok, tok)
                    token_counts[display_tok] = val

            sql_nulls = total_rows - non_null_count
            pseudo_nulls = sum(token_counts.values())
            total_nulls = min(total_rows, sql_nulls + pseudo_nulls)
            null_pct = round((total_nulls / total_rows) * 100, 2)

            if total_nulls == 0 and total_rows > 1:
                if total_rows <= 100000:
                    is_candidate_key = distinct_count == total_rows
                elif distinct_count >= int(total_rows * 0.95):
                    has_dup = conn.execute(
                        f"SELECT 1 FROM (SELECT hash({quoted}) AS _h FROM {view_name} GROUP BY _h HAVING COUNT(*) > 1 LIMIT 1)"
                    ).fetchone()
                    is_candidate_key = has_dup is None
                    if is_candidate_key:
                        distinct_count = total_rows
                else:
                    is_candidate_key = False
            else:
                is_candidate_key = False

            phase1_meta[col_name] = {
                "non_null_count": non_null_count,
                "distinct_count": distinct_count,
                "ws_issues": ws_issues,
                "token_counts": token_counts,
                "total_nulls": total_nulls,
                "null_pct": null_pct,
                "is_candidate_key": is_candidate_key,
            }

        # Phase 2: Batched type inference on a single sample window
        SAMPLE_SIZE = 100000
        batch_select_cols = ", ".join(self._quote_ident(c) for c in batch_cols)
        sample_rel = (
            f"(SELECT {batch_select_cols} FROM {view_name} LIMIT {SAMPLE_SIZE})"
            if total_rows > SAMPLE_SIZE
            else view_name
        )

        p2_exprs: List[str] = []
        for idx, col_name in enumerate(batch_cols):
            quoted = self._quote_ident(col_name)
            str_val = f"try_cast({quoted} AS VARCHAR)"
            valid_cond = f"{quoted} IS NOT NULL AND lower(trim({str_val})) NOT IN ({null_token_sql})"
            p2_exprs.append(f"COUNT(try_cast({quoted} AS BIGINT)) FILTER (WHERE {valid_cond}) AS c{idx}_int")
            p2_exprs.append(f"COUNT(try_cast({quoted} AS DOUBLE)) FILTER (WHERE {valid_cond}) AS c{idx}_dbl")
            p2_exprs.append(f"COUNT(try_cast({quoted} AS BOOLEAN)) FILTER (WHERE {valid_cond}) AS c{idx}_bool")
            p2_exprs.append(f"COUNT(*) FILTER (WHERE {valid_cond}) AS c{idx}_valid")

        p2_row = conn.execute(f"SELECT {', '.join(p2_exprs)} FROM {sample_rel};").fetchone()

        inferred_types: Dict[str, str] = {}
        sample_valids: Dict[str, int] = {}
        int_cols: List[str] = []
        dbl_cols: List[str] = []
        other_cols: List[str] = []

        for idx, col_name in enumerate(batch_cols):
            base = idx * 4
            int_valid = p2_row[base] or 0
            dbl_valid = p2_row[base + 1] or 0
            bool_valid = p2_row[base + 2] or 0
            sample_valid = p2_row[base + 3] or 0

            # Fallback if leading SAMPLE_SIZE rows happened to be all null while non-null rows exist later
            if sample_valid == 0 and total_rows > SAMPLE_SIZE and phase1_meta[col_name]["non_null_count"] > 0:
                quoted = self._quote_ident(col_name)
                str_val = f"try_cast({quoted} AS VARCHAR)"
                fb_q = f"""
                    SELECT
                        COUNT(try_cast({quoted} AS BIGINT)),
                        COUNT(try_cast({quoted} AS DOUBLE)),
                        COUNT(try_cast({quoted} AS BOOLEAN)),
                        COUNT(*)
                    FROM (SELECT {quoted} FROM {view_name} WHERE {quoted} IS NOT NULL LIMIT {SAMPLE_SIZE})
                    WHERE lower(trim({str_val})) NOT IN ({null_token_sql});
                """
                fb_row = conn.execute(fb_q).fetchone()
                int_valid = fb_row[0] or 0
                dbl_valid = fb_row[1] or 0
                bool_valid = fb_row[2] or 0
                sample_valid = fb_row[3] or 0

            sample_valids[col_name] = sample_valid
            if sample_valid > 0 and (int_valid / sample_valid) >= 0.85:
                inferred_types[col_name] = "integer"
                int_cols.append(col_name)
            elif sample_valid > 0 and (dbl_valid / sample_valid) >= 0.85:
                inferred_types[col_name] = "double"
                dbl_cols.append(col_name)
            elif sample_valid > 0 and (bool_valid / sample_valid) >= 0.95:
                inferred_types[col_name] = "boolean"
            else:
                inferred_types[col_name] = "string"
                other_cols.append(col_name)

        # Phase 3a: Batch numeric stats for integers and doubles (1 scan per numeric type in batch)
        numeric_stats_map: Dict[str, NumericStats] = {}
        for num_cols, sql_cast, precision in (
            (int_cols, "BIGINT", None),
            (dbl_cols, "DOUBLE", 4),
        ):
            if not num_cols:
                continue
            num_exprs: List[str] = []
            for idx, col_name in enumerate(num_cols):
                quoted = self._quote_ident(col_name)
                cast_expr = f"try_cast({quoted} AS {sql_cast})"
                med_expr = (
                    f"MEDIAN({cast_expr})"
                    if total_rows <= 100000
                    else f"APPROX_QUANTILE({cast_expr}, 0.5)"
                )
                num_exprs.extend([
                    f"MIN({cast_expr}) AS n{idx}_min",
                    f"MAX({cast_expr}) AS n{idx}_max",
                    f"{med_expr} AS n{idx}_med",
                    f"SUM(CASE WHEN {cast_expr} < 0 THEN 1 ELSE 0 END) AS n{idx}_neg",
                ])
            num_row = conn.execute(f"SELECT {', '.join(num_exprs)} FROM {view_name};").fetchone()
            for idx, col_name in enumerate(num_cols):
                base = idx * 4
                meta = phase1_meta[col_name]
                neg_count = num_row[base + 3] or 0
                valid_candidates = max(
                    1,
                    meta["non_null_count"] - sum(c for k, c in meta["token_counts"].items() if k != "<empty>"),
                )
                v_min = float(num_row[base]) if num_row[base] is not None else None
                v_max = float(num_row[base + 1]) if num_row[base + 1] is not None else None
                v_med = float(num_row[base + 2]) if num_row[base + 2] is not None else None
                if precision is not None:
                    v_min = round(v_min, precision) if v_min is not None else None
                    v_max = round(v_max, precision) if v_max is not None else None
                    v_med = round(v_med, precision) if v_med is not None else None
                numeric_stats_map[col_name] = NumericStats(
                    min=v_min,
                    max=v_max,
                    median=v_med,
                    negatives=neg_count,
                    negative_pct=round((neg_count / valid_candidates) * 100, 2),
                )

        # Phase 3b: Date / timestamp checks on non-numeric non-bool columns (on bounded sample)
        formats_map: Dict[str, List[DateFormatMatch]] = {}
        tz_map: Dict[str, Optional[str]] = {}
        amb_map: Dict[str, bool] = {}

        for col_name in other_cols:
            sample_tested = sample_valids[col_name]
            if sample_tested <= 0:
                continue
            quoted = self._quote_ident(col_name)
            col_sample = (
                f"(SELECT {quoted} FROM {view_name} WHERE {quoted} IS NOT NULL LIMIT {SAMPLE_SIZE})"
                if total_rows > SAMPLE_SIZE
                else f"(SELECT {quoted} FROM {view_name} WHERE {quoted} IS NOT NULL)"
            )
            fmt_cases = ", ".join(
                [f"COUNT(try_strptime(trim({quoted}), '{fmt}')) AS fmt_{i}" for i, fmt in enumerate(COMMON_DATE_FORMATS)]
            )
            try:
                date_res = conn.execute(f"SELECT {fmt_cases} FROM {col_sample};").fetchone()
                matched_fmts: List[DateFormatMatch] = []
                for i, fmt in enumerate(COMMON_DATE_FORMATS):
                    matched = date_res[i] or 0
                    pct = round((matched / sample_tested) * 100, 2)
                    if pct >= 2.0:
                        matched_fmts.append(DateFormatMatch(format=fmt, match_pct=pct))
                if matched_fmts:
                    inferred_types[col_name] = "timestamp"
                    matched_fmts.sort(key=lambda x: x.match_pct, reverse=True)
                    formats_map[col_name] = matched_fmts

                    tz_res = conn.execute(
                        f"""
                        SELECT
                            SUM(CASE WHEN {quoted} LIKE '%Z' THEN 1 ELSE 0 END),
                            SUM(CASE WHEN {quoted} ~ '[+-][0-9]{{2}}:[0-9]{{2}}$' THEN 1 ELSE 0 END)
                        FROM {col_sample};
                        """
                    ).fetchone()
                    z_cnt = tz_res[0] or 0
                    offset_cnt = tz_res[1] or 0
                    if z_cnt > 0 and offset_cnt == 0:
                        tz_map[col_name] = "UTC (Z suffix)"
                    elif offset_cnt > 0 and z_cnt == 0:
                        tz_map[col_name] = "offset"
                    elif offset_cnt > 0 and z_cnt > 0:
                        tz_map[col_name] = "mixed"
                    else:
                        tz_map[col_name] = "naive"

                    amb_res = conn.execute(
                        f"""
                        SELECT
                            COUNT(try_strptime({quoted}, '%d/%m/%Y')),
                            COUNT(try_strptime({quoted}, '%m/%d/%Y'))
                        FROM {col_sample}
                        WHERE {quoted} LIKE '%/%/%';
                        """
                    ).fetchone()
                    dm_c = amb_res[0] or 0
                    md_c = amb_res[1] or 0
                    if dm_c > 0 and dm_c == md_c:
                        amb_map[col_name] = True
            except Exception as e:
                logger.debug("Date format scan skipped for %s: %s", col_name, e)

        # Phase 4: Sample values (bounded window of 2000 rows)
        results: Dict[str, ColumnProfile] = {}
        for col_name in batch_cols:
            sample_values: List[str] = []
            if not self.privacy_mode:
                quoted = self._quote_ident(col_name)
                str_val = f"try_cast({quoted} AS VARCHAR)"
                sample_q = f"""
                    SELECT DISTINCT {quoted}
                    FROM (SELECT {quoted} FROM {view_name} WHERE {quoted} IS NOT NULL LIMIT 2000)
                    WHERE lower(trim({str_val})) NOT IN ({null_token_sql})
                    LIMIT 5;
                """
                sample_rows = conn.execute(sample_q).fetchall()
                sample_values = [str(r[0]) for r in sample_rows]

            meta = phase1_meta[col_name]
            results[col_name] = ColumnProfile(
                name=col_name,
                inferred_type=inferred_types[col_name],
                total_count=total_rows,
                nulls=meta["total_nulls"],
                null_pct=meta["null_pct"],
                null_tokens=meta["token_counts"],
                distinct_count=meta["distinct_count"],
                candidate_key=meta["is_candidate_key"],
                whitespace_issues=meta["ws_issues"],
                casing_issues=0,
                numeric_stats=numeric_stats_map.get(col_name),
                formats=formats_map.get(col_name, []),
                timezone_evidence=tz_map.get(col_name),
                day_month_ambiguous=amb_map.get(col_name, False),
                sample_values=sample_values,
            )

        return results

    def _profile_column(
        self,
        conn: duckdb.DuckDBPyConnection,
        view_name: str,
        col_name: str,
        total_rows: int,
    ) -> ColumnProfile:
        """Inspect and compute statistical distributions for a single column."""
        if total_rows == 0:
            return ColumnProfile(
                name=col_name,
                inferred_type="string",
                total_count=0,
                nulls=0,
                null_pct=0.0,
            )
        return self._profile_columns_batch(conn, view_name, [col_name], total_rows)[col_name]
