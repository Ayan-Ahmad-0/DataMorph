"""SQL emitter generating readable CTE-chain SQL across supported dialects."""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from core.export.base import EmitterOutput
from core.export.dialects import transpile_duckdb_sql
from core.export.headers import build_export_header, sanitize_identifier
from core.export.ir import IRStep, RecipeIR
from core.export.models import ExportOptions, ExportedFile
from core.macros.base import quote_ident
from core.macros.dedupe import DedupeMacro
from core.macros.privacy import MaskPiiMacro
from core.macros.strings import TrimAndNormalizeMacro

_SIMPLE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SOURCE_PLACEHOLDER = "__datamorph_source_placeholder__"


def _format_source_ref(source_name: Optional[str]) -> str:
    if not source_name:
        return _SOURCE_PLACEHOLDER
    parts = source_name.split(".")
    formatted = [p if _SIMPLE_IDENT_RE.match(p) else quote_ident(p) for p in parts]
    return ".".join(formatted)


def _format_sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def _render_normalize_expr(col: str, steps: List[IRStep]) -> str:
    expr = quote_ident(col)
    for step in steps:
        if step.macro == "trim_and_normalize":
            macro = TrimAndNormalizeMacro(
                trim=bool(step.options.get("trim", True)),
                case=step.options.get("case"),
                null_tokens=list(step.options.get("null_tokens") or []),
            )
            expr = macro.to_sql(col)
        elif step.macro == "parse_timestamp":
            formats = step.options.get("formats", [])
            calls = [f"try_strptime(trim({expr}), '{fmt}')" for fmt in formats]
            expr = f"COALESCE({', '.join(calls)})" if len(calls) > 1 else calls[0]
        elif step.macro == "cast_type":
            target_type = str(step.options["type"]).upper()
            expr = f"try_cast({expr} AS {target_type})"
        elif step.macro == "clip_outliers":
            num_expr = f"try_cast({expr} AS DOUBLE)"
            min_v = step.options.get("min")
            max_v = step.options.get("max")
            action = step.options.get("action", "null")
            if action == "null":
                conds = []
                if min_v is not None:
                    conds.append(f"{num_expr} < {min_v}")
                if max_v is not None:
                    conds.append(f"{num_expr} > {max_v}")
                expr = f"CASE WHEN {' OR '.join(conds)} THEN NULL ELSE {expr} END"
            elif action == "clamp":
                whens = []
                if min_v is not None:
                    whens.append(f"WHEN {num_expr} < {min_v} THEN {min_v}")
                if max_v is not None:
                    whens.append(f"WHEN {num_expr} > {max_v} THEN {max_v}")
                expr = f"CASE {' '.join(whens)} ELSE {expr} END"
            else:
                raise ValueError(f"Unsupported clip_outliers action: {action}")
        else:
            raise ValueError(f"Unexpected normalize macro: {step.macro}")
    return expr


def _render_projection_cte(
    cte_name: str,
    prev_cte: str,
    ir: RecipeIR,
    replacements: Dict[str, str],
) -> str:
    """Emit explicit column projections when full column list is known, else * REPLACE."""
    if ir.has_full_column_list and ir.columns:
        selects = []
        for col in ir.columns:
            qcol = quote_ident(col)
            if col in replacements:
                selects.append(f"        {replacements[col]} AS {qcol}")
            else:
                selects.append(f"        {qcol}")
        body = ",\n".join(selects)
        return f"{cte_name} AS (\n    SELECT\n{body}\n    FROM {prev_cte}\n)"

    replace_items = [f"{expr} AS {quote_ident(col)}" for col, expr in replacements.items()]
    return f"{cte_name} AS (\n    SELECT * REPLACE ({', '.join(replace_items)})\n    FROM {prev_cte}\n)"


def render_duckdb_sql_body(
    ir: RecipeIR,
    *,
    source_expr: str,
) -> Tuple[str, List[str]]:
    """Build DuckDB CTE-chain SQL body (using source_expr) and collect parity warnings."""
    warnings: List[str] = list(ir.ir_warnings)
    ctes: List[str] = [f"WITH staged AS (\n    SELECT *\n    FROM {source_expr}\n)"]

    # Group normalize steps by column
    norm_by_col: Dict[str, List[IRStep]] = {}
    has_title_case = False
    for step in ir.steps_for_stage("normalize"):
        if step.column:
            norm_by_col.setdefault(step.column, []).append(step)
            if step.macro == "trim_and_normalize" and step.options.get("case") == "title":
                has_title_case = True

    if has_title_case:
        warnings.append(
            "SQL title-casing uppercases only the first character of the value "
            "(matching DataMorph DuckDB semantics), unlike word-level title-case in Python."
        )

    # 1. normalized CTE
    if ir.columns:
        norm_selects = []
        for col in ir.columns:
            col_steps = norm_by_col.get(col, [])
            expr = _render_normalize_expr(col, col_steps) if col_steps else quote_ident(col)
            norm_selects.append(f"        {expr} AS {quote_ident(col)}")
        cte_normalized = "normalized AS (\n    SELECT\n" + ",\n".join(norm_selects) + "\n    FROM staged\n)"
    else:
        cte_normalized = "normalized AS (\n    SELECT *\n    FROM staged\n)"
    ctes.append(cte_normalized)
    last_cte = "normalized"

    # 2. filtered CTE (handle_nulls drop_row)
    filter_steps = ir.steps_for_stage("filter")
    if filter_steps:
        where_parts = [f"{quote_ident(s.column)} IS NOT NULL" for s in filter_steps if s.column]
        if where_parts:
            select_cols = ", ".join(quote_ident(c) for c in ir.columns) if (ir.has_full_column_list and ir.columns) else "*"
            ctes.append(
                f"filtered AS (\n    SELECT {select_cols}\n    FROM {last_cte}\n    WHERE {' AND '.join(where_parts)}\n)"
            )
            last_cte = "filtered"

    # 3. filled CTE (handle_nulls fill_*)
    impute_steps = ir.steps_for_stage("impute")
    impute_replacements: Dict[str, str] = {}
    for step in impute_steps:
        if not step.column:
            continue
        qcol = quote_ident(step.column)
        strat = step.options.get("strategy")
        if strat == "fill_median":
            impute_replacements[step.column] = f"COALESCE({qcol}, (SELECT MEDIAN({qcol}) FROM normalized))"
        elif strat == "fill_mode":
            impute_replacements[step.column] = f"COALESCE({qcol}, (SELECT MODE({qcol}) FROM normalized))"
        elif strat == "fill_value":
            val_repr = _format_sql_literal(step.options.get("value"))
            impute_replacements[step.column] = f"COALESCE({qcol}, {val_repr})"
        elif strat == "flag":
            continue
        else:
            raise ValueError(f"Unsupported handle_nulls strategy: {strat}")

    if impute_replacements:
        ctes.append(_render_projection_cte("filled", last_cte, ir, impute_replacements))
        last_cte = "filled"

    # 4. masked CTE (mask_pii)
    mask_steps = ir.steps_for_stage("mask")
    mask_replacements: Dict[str, str] = {}
    for step in mask_steps:
        if not step.column:
            continue
        macro = MaskPiiMacro(
            strategy=step.options.get("strategy", "partial_mask"),
            entity_type=step.options.get("entity_type"),
        )
        mask_replacements[step.column] = macro.to_sql(step.column)

    if mask_replacements:
        ctes.append(_render_projection_cte("masked", last_cte, ir, mask_replacements))
        last_cte = "masked"

    # 5. Final SELECT + optional QUALIFY dedupe
    final_cols = ", ".join(quote_ident(c) for c in ir.columns) if (ir.has_full_column_list and ir.columns) else "*"
    final_query = f"SELECT {final_cols}\nFROM {last_cte}"

    dedupe_steps = ir.steps_for_stage("dedupe")
    if dedupe_steps:
        d_opts = dedupe_steps[-1].options
        dedupe_macro = DedupeMacro(
            keys=list(d_opts["keys"]),
            keep=d_opts.get("keep", "latest"),
            order_by=d_opts.get("order_by"),
        )
        final_query += f"\n{dedupe_macro.to_qualify_clause()}"

    sql_body = ",\n".join(ctes) + "\n" + final_query + ";"
    return sql_body, warnings


class SqlEmitter:
    """Emits standalone SQL scripts targeting DuckDB, Snowflake, BigQuery, or Postgres."""

    name: str = "sql"
    file_extension: str = ".sql"

    def emit(self, ir: RecipeIR, options: ExportOptions) -> EmitterOutput:
        return self.emit_with_source(
            ir,
            options,
            source_token=options.source_name and _format_source_ref(options.source_name) or "{{ source_table }}",
            filename=f"clean_{sanitize_identifier(options.dataset_name, fallback='dataset')}.sql"
            if options.dataset_name
            else "clean.sql",
        )

    def emit_with_source(
        self,
        ir: RecipeIR,
        options: ExportOptions,
        *,
        source_token: str,
        filename: str,
        target_label: str = "sql",
    ) -> EmitterOutput:
        use_placeholder = "{{" in source_token
        compile_source = _SOURCE_PLACEHOLDER if use_placeholder else source_token

        duckdb_sql, warnings = render_duckdb_sql_body(ir, source_expr=compile_source)
        transpiled_sql, dialect_warnings = transpile_duckdb_sql(duckdb_sql, options.dialect)
        warnings.extend(dialect_warnings)

        if use_placeholder:
            transpiled_sql = (
                transpiled_sql
                .replace(f'"{_SOURCE_PLACEHOLDER}"', source_token)
                .replace(f"`{_SOURCE_PLACEHOLDER}`", source_token)
                .replace(_SOURCE_PLACEHOLDER, source_token)
            )

        header = build_export_header(
            target=target_label,
            job_id=options.job_id,
            timestamp_iso=options.utc_timestamp_iso(),
            dialect=options.dialect,
            dataset_name=options.dataset_name,
            caveats=warnings,
            comment_prefix="-- ",
        )
        content = f"{header}\n\n{transpiled_sql.rstrip()}\n"
        return EmitterOutput(
            files=[ExportedFile(name=filename, content=content, language="sql")],
            warnings=warnings,
        )
