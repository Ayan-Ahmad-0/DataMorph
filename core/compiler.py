"""Recipe to SQL compiler and DuckDB cleaning execution engine."""

from __future__ import annotations
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
import duckdb

from core.duckdb_utils import (
    configure_duckdb_connection,
    get_duckdb_memory_limit,
    get_duckdb_temp_directory,
    get_duckdb_threads,
)

from core.macros.base import quote_ident
from core.macros.dedupe import DedupeMacro
from core.macros.strings import TrimAndNormalizeMacro
from core.macros.numbers import CastTypeMacro, ClipOutliersMacro, HandleNullsMacro
from core.macros.dates import ParseTimestampMacro
from core.macros.privacy import MaskPiiMacro
from core.recipe_schema import Recipe
from core.validator import RecipeValidator

logger = logging.getLogger(__name__)


@dataclass
class CleaningResult:
    input_parquet: str
    output_parquet: str
    rows_in: int
    rows_out: int
    rows_dropped: int
    duration_seconds: float
    output_size_bytes: int
    compiled_sql: str
    changes_summary: Dict[str, Any]


class RecipeCompiler:
    """Compiles validated recipes into high-performance CTE-chain DuckDB SQL."""

    def __init__(
        self,
        memory_limit: Optional[str] = None,
        threads: Optional[int] = None,
        temp_directory: Optional[str] = None,
    ) -> None:
        self.memory_limit = memory_limit or get_duckdb_memory_limit()
        self.threads = threads or get_duckdb_threads()
        self.temp_directory = temp_directory or get_duckdb_temp_directory()

    def compile_to_sql(
        self,
        recipe: Recipe,
        input_parquet_path: str,
        all_columns: List[str],
    ) -> str:
        """Render a single CTE-chain SQL statement executing the recipe."""
        in_sql_path = input_parquet_path.replace("\\", "/")

        # 1. Stage CTE
        cte_staged = f"WITH staged AS (\n    SELECT * FROM read_parquet('{in_sql_path}')\n)"

        # 2. Normalized CTE
        norm_selects = []
        for col in all_columns:
            rule = recipe.columns.get(col)
            expr = quote_ident(col)

            if rule:
                # String trim & token replacement
                if rule.trim_and_normalize:
                    macro = TrimAndNormalizeMacro(
                        trim=rule.trim_and_normalize.trim,
                        case=rule.trim_and_normalize.case,
                        null_tokens=rule.trim_and_normalize.null_tokens,
                    )
                    expr = macro.to_sql(col)

                # Timestamp parsing
                if rule.parse_timestamp:
                    # Ingest the expr into timestamp parser
                    fmts = rule.parse_timestamp.formats
                    calls = [f"try_strptime(trim({expr}), '{fmt}')" for fmt in fmts]
                    expr = f"COALESCE({', '.join(calls)})" if len(calls) > 1 else calls[0]

                # Cast type
                if rule.cast_type:
                    expr = f"try_cast({expr} AS {rule.cast_type.type.upper()})"

                # Clip outliers
                if rule.clip_outliers:
                    cfg = rule.clip_outliers
                    num_expr = f"try_cast({expr} AS DOUBLE)"
                    if cfg.action == "null":
                        conds = []
                        if cfg.min is not None:
                            conds.append(f"{num_expr} < {cfg.min}")
                        if cfg.max is not None:
                            conds.append(f"{num_expr} > {cfg.max}")
                        or_cond = " OR ".join(conds)
                        expr = f"CASE WHEN {or_cond} THEN NULL ELSE {expr} END"
                    elif cfg.action == "clamp":
                        whens = []
                        if cfg.min is not None:
                            whens.append(f"WHEN {num_expr} < {cfg.min} THEN {cfg.min}")
                        if cfg.max is not None:
                            whens.append(f"WHEN {num_expr} > {cfg.max} THEN {cfg.max}")
                        expr = f"CASE {' '.join(whens)} ELSE {expr} END"

            norm_selects.append(f"        {expr} AS {quote_ident(col)}")

        cte_normalized = "normalized AS (\n    SELECT\n" + ",\n".join(norm_selects) + "\n    FROM staged\n)"

        # 3. Filter CTE (for handle_nulls drop_row)
        drop_filters = []
        for col, rule in recipe.columns.items():
            if rule.handle_nulls and rule.handle_nulls.strategy == "drop_row":
                drop_filters.append(f"{quote_ident(col)} IS NOT NULL")

        last_cte_name = "normalized"
        ctes = [cte_staged, cte_normalized]

        if drop_filters:
            where_clause = " AND ".join(drop_filters)
            cte_filtered = f"filtered AS (\n    SELECT * FROM {last_cte_name} WHERE {where_clause}\n)"
            ctes.append(cte_filtered)
            last_cte_name = "filtered"

        # 4. Filled CTE (for imputation: fill_median, fill_mode, fill_value)
        #    Precompute all aggregate scalars (MEDIAN/MODE) in a dedicated scalar CTE
        #    so DuckDB materialises them once rather than re-scanning `normalized`
        #    for every imputed column.
        # col -> scalar alias; values are scalar subqueries so _impute_scalars
        # always returns exactly one row with no FROM clause.
        scalar_defs: Dict[str, str] = {}
        impute_replaces = []
        for col, rule in recipe.columns.items():
            if rule.handle_nulls:
                strat = rule.handle_nulls.strategy
                qcol = quote_ident(col)
                if strat == "fill_median":
                    alias = f"_med_{col}"
                    scalar_defs[alias] = f"(SELECT MEDIAN({qcol}) FROM {last_cte_name})"
                    impute_replaces.append(f"COALESCE({qcol}, (SELECT {alias} FROM _impute_scalars)) AS {qcol}")
                elif strat == "fill_mode":
                    # Deterministic tie-break: most frequent non-null value,
                    # alphabetically first when counts are equal.
                    alias = f"_mode_{col}"
                    scalar_defs[alias] = (
                        f"(SELECT {qcol} FROM {last_cte_name} "
                        f"WHERE {qcol} IS NOT NULL "
                        f"GROUP BY {qcol} "
                        f"ORDER BY COUNT(*) DESC, {qcol} ASC "
                        f"LIMIT 1)"
                    )
                    impute_replaces.append(f"COALESCE({qcol}, (SELECT {alias} FROM _impute_scalars)) AS {qcol}")
                elif strat == "fill_value":
                    val = rule.handle_nulls.value
                    val_str = f"'{val}'" if isinstance(val, str) else str(val)
                    impute_replaces.append(f"COALESCE({qcol}, {val_str}) AS {qcol}")

        if impute_replaces:
            if scalar_defs:
                # All exprs are scalar subqueries — no FROM needed; always 1 row.
                scalar_select = ", ".join(f"{expr} AS {alias}" for alias, expr in scalar_defs.items())
                cte_scalars = f"_impute_scalars AS (\n    SELECT {scalar_select}\n)"
                ctes.append(cte_scalars)
            replace_expr = ", ".join(impute_replaces)
            cte_filled = f"filled AS (\n    SELECT * REPLACE ({replace_expr}) FROM {last_cte_name}\n)"
            ctes.append(cte_filled)
            last_cte_name = "filled"

        # 5. Masking CTE (for GDPR/HIPAA mask_pii)
        mask_replaces = []
        for col, rule in recipe.columns.items():
            if rule.mask_pii:
                macro = MaskPiiMacro(
                    strategy=rule.mask_pii.strategy,
                    entity_type=rule.mask_pii.entity_type,
                )
                mask_expr = macro.to_sql(col)
                mask_replaces.append(f"{mask_expr} AS {quote_ident(col)}")

        if mask_replaces:
            replace_expr = ", ".join(mask_replaces)
            cte_masked = f"masked AS (\n    SELECT * REPLACE ({replace_expr}) FROM {last_cte_name}\n)"
            ctes.append(cte_masked)
            last_cte_name = "masked"

        # 6. Final query with Dedupe qualify
        final_query = f"SELECT *\nFROM {last_cte_name}"
        if recipe.dedupe:
            dedupe_macro = DedupeMacro(
                keys=recipe.dedupe.keys,
                keep=recipe.dedupe.keep,
                order_by=recipe.dedupe.order_by,
            )
            final_query += f"\n{dedupe_macro.to_qualify_clause()}"

        return ",\n".join(ctes) + "\n" + final_query + ";"

    def execute_clean(
        self,
        recipe: Recipe,
        input_parquet: str | Path,
        output_parquet: str | Path,
    ) -> CleaningResult:
        """Execute compiled cleaning SQL on DuckDB and stream out cleaned Parquet."""
        start_time = time.perf_counter()
        in_path = Path(input_parquet).resolve()
        out_path = Path(output_parquet).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if not in_path.exists():
            raise FileNotFoundError(f"Input Parquet not found: {in_path}")

        in_sql_path = str(in_path).replace("\\", "/")
        out_sql_path = str(out_path).replace("\\", "/")

        conn = duckdb.connect(":memory:")
        configure_duckdb_connection(
            conn,
            memory_limit=self.memory_limit,
            threads=self.threads,
            temp_directory=self.temp_directory,
        )

        try:
            # 1. Discover columns and initial row count
            col_info = conn.execute(f"DESCRIBE SELECT * FROM read_parquet('{in_sql_path}')").fetchall()
            all_columns = [col[0] for col in col_info]
            rows_in = conn.execute(f"SELECT COUNT(*) FROM read_parquet('{in_sql_path}')").fetchone()[0]

            # 2. Compile SQL
            compiled_sql = self.compile_to_sql(recipe, str(in_path), all_columns)

            # Remove trailing semicolon for wrapping in COPY
            core_query = compiled_sql.rstrip().rstrip(";")
            copy_sql = f"COPY ({core_query}) TO '{out_sql_path}' (FORMAT PARQUET, COMPRESSION ZSTD);"

            # 3. Execute
            conn.execute(copy_sql)

            # 4. Measure output and change breakdown
            rows_out = conn.execute(f"SELECT COUNT(*) FROM read_parquet('{out_sql_path}')").fetchone()[0]
            elapsed = time.perf_counter() - start_time
            out_bytes = out_path.stat().st_size if out_path.exists() else 0
            rows_removed = max(0, rows_in - rows_out)

            # Breakdown by reason
            dedupe_removed = 0
            if recipe.dedupe:
                dedupe_removed = rows_removed

            changes_summary = {
                "rows_in": rows_in,
                "rows_out": rows_out,
                "rows_removed_total": rows_removed,
                "rows_removed_by_reason": {
                    "deduplication": dedupe_removed,
                    "null_filtering": max(0, rows_removed - dedupe_removed),
                },
                "columns_transformed": list(recipe.columns.keys()),
                "rules_applied_count": len(recipe.columns) + (1 if recipe.dedupe else 0),
            }

            return CleaningResult(
                input_parquet=str(in_path),
                output_parquet=str(out_path),
                rows_in=rows_in,
                rows_out=rows_out,
                rows_dropped=rows_removed,
                duration_seconds=round(elapsed, 3),
                output_size_bytes=out_bytes,
                compiled_sql=compiled_sql,
                changes_summary=changes_summary,
            )
        finally:
            conn.close()
