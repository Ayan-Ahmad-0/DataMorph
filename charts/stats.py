"""Quick column statistics and kind classification for raw uncleaned data."""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import duckdb

from charts.compile import quote_identifier
from charts.limits import ChartLimits
from explore.limits import QueryLimits
from explore.sandbox import create_sandboxed_connection


@dataclass
class ColumnStats:
    name: str
    type: str                  # DuckDB type (e.g. VARCHAR, BIGINT)
    null_pct: float
    distinct_approx: Optional[int] = None
    min: Optional[str] = None         # stringified for display
    max: Optional[str] = None
    kind: str = "categorical"         # "numeric" | "categorical" | "datetime" | "boolean" | "text_long" | "id_like"
    parse_hints: Dict[str, Any] = field(default_factory=dict)   # {"as_date_success_pct": 96.4, "as_number_success_pct": 0.0}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "null_pct": self.null_pct,
            "distinct_approx": self.distinct_approx,
            "min": self.min,
            "max": self.max,
            "kind": self.kind,
            "parse_hints": self.parse_hints,
        }


def compute_column_stats(parquet_path: Path, limits: Optional[ChartLimits] = None) -> List[ColumnStats]:
    """Compute column statistics, kind classifications, and parse hints on raw Parquet data using DuckDB.
    
    Fast server-side analytical summary using DuckDB queries with approximate counts and null percentages.
    """
    lim = limits or ChartLimits()
    q_lim = QueryLimits(
        query_timeout_seconds=lim.stats_query_timeout_seconds,
        memory_limit_per_query="2GB",
    )
    con = create_sandboxed_connection(parquet_path, limits=q_lim)

    try:
        # 1. Total row count
        total_rows = con.execute("SELECT COUNT(*) FROM data").fetchone()[0]
        if total_rows == 0:
            return []

        # 2. Get column metadata
        desc = con.execute("DESCRIBE data").fetchall()
        columns: List[Tuple[str, str]] = [(str(row[0]), str(row[1])) for row in desc]

        stats_list: List[ColumnStats] = []

        for col_name, col_type in columns:
            col_q = quote_identifier(col_name)
            col_upper = col_type.upper()

            # Base stats: null count, approx distinct
            query = f"""
            SELECT 
                COUNT(*) - COUNT({col_q}) AS null_cnt,
                approx_count_distinct({col_q}) AS approx_distinct,
                MIN(CAST({col_q} AS VARCHAR)) AS min_val,
                MAX(CAST({col_q} AS VARCHAR)) AS max_val
            FROM data
            """
            null_cnt, approx_dist, min_v, max_v = con.execute(query).fetchone()

            null_pct = round((float(null_cnt) / float(total_rows)) * 100.0, 1) if total_rows > 0 else 0.0
            approx_dist_int = int(approx_dist) if approx_dist is not None else 0

            # Classify kind
            is_bool = col_upper.startswith("BOOL")
            is_num = any(col_upper.startswith(p) for p in (
                "INT", "BIGINT", "SMALLINT", "TINYINT", "HUGEINT", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "REAL"
            ))
            is_dt = any(kw in col_upper for kw in ("DATE", "TIME", "TIMESTAMP"))

            parse_hints: Dict[str, Any] = {}

            if is_bool:
                kind = "boolean"
            elif is_num:
                # If distinct is nearly 100% of rows and integer-like, might be id-like
                if total_rows > 100 and approx_dist_int > 0.95 * total_rows:
                    kind = "id_like"
                else:
                    kind = "numeric"
            elif is_dt:
                kind = "datetime"
            else:
                # String / text / other column
                # Check parse hints on a small sample for raw files
                hint_query = f"""
                WITH s AS (
                    SELECT {col_q} AS val
                    FROM data
                    WHERE {col_q} IS NOT NULL
                    LIMIT 200
                )
                SELECT 
                    COUNT(*) AS non_null_sample,
                    SUM(CASE WHEN TRY_CAST(val AS DOUBLE) IS NOT NULL THEN 1 ELSE 0 END) AS num_parsed,
                    SUM(CASE WHEN TRY_CAST(val AS TIMESTAMP) IS NOT NULL THEN 1 ELSE 0 END) AS date_parsed,
                    AVG(LENGTH(CAST(val AS VARCHAR))) AS avg_len
                FROM s
                """
                s_res = con.execute(hint_query).fetchone()
                non_null_s = s_res[0] or 0
                num_parsed = s_res[1] or 0
                date_parsed = s_res[2] or 0
                avg_len = s_res[3] or 0.0

                num_pct = round((num_parsed / non_null_s) * 100.0, 1) if non_null_s > 0 else 0.0
                date_pct = round((date_parsed / non_null_s) * 100.0, 1) if non_null_s > 0 else 0.0

                parse_hints = {
                    "as_number_success_pct": num_pct,
                    "as_date_success_pct": date_pct,
                }

                if date_pct >= 85.0:
                    kind = "datetime"
                elif num_pct >= 85.0:
                    kind = "numeric"
                elif avg_len > 100:
                    kind = "text_long"
                elif approx_dist_int > 0.95 * total_rows and total_rows > 100:
                    kind = "id_like"
                elif approx_dist_int <= 50:
                    kind = "categorical"
                else:
                    kind = "categorical"

            stats_list.append(ColumnStats(
                name=col_name,
                type=col_type,
                null_pct=null_pct,
                distinct_approx=approx_dist_int,
                min=str(min_v) if min_v is not None else None,
                max=str(max_v) if max_v is not None else None,
                kind=kind,
                parse_hints=parse_hints,
            ))

        return stats_list
    finally:
        con.close()
